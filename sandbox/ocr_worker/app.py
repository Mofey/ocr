"""Small, isolated OCR service. This module has no database or R2 client imports."""
from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import os
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .grant import OCRInputGrant, OCRJobGrantError, TransientReceiptGrant, verify_ocr_job_grant

logger = logging.getLogger("ocr_worker")
MAX_OCR_INPUT_BYTES = 2 * 1024 * 1024
MAX_WORKER_JOB_SECONDS = 30
MAX_OUTPUT_TEXT_CHARS = 200_000
MAX_OUTPUT_LAYOUT_LINES = 500
MAX_CHILD_IPC_BYTES = 1024 * 1024
_active_job = False
_signing_secret = ""
_runtime_ready = False


def safe_failure_diagnostic(result: dict) -> dict:
    # The native parser handles untrusted content. Only these fixed values may
    # cross into operational logs; exception messages and traces never do.
    stages = {"parser_startup", "pdf_validation", "image_validation", "model_initialization",
              "pdf_render", "image_recognition", "structured_extraction", "child_exit", "child_ipc", "result_validation"}
    error_types = {"RuntimeError", "ValueError", "TypeError", "AttributeError", "OSError", "PermissionError",
                   "FileNotFoundError", "MemoryError", "ImportError", "ModuleNotFoundError", "NotImplementedError",
                   "AssertionError", "SystemError", "ProcessExit", "TimeoutError"}
    stage, error_type = result.get("stage"), result.get("errorType")
    diagnostic = {
        "stage": stage if isinstance(stage, str) and stage in stages else "parser_startup",
        "errorType": error_type if isinstance(error_type, str) and error_type in error_types else "ParserError",
    }
    exit_code = result.get("exitCode")
    if type(exit_code) is int and -128 <= exit_code <= 255:
        diagnostic["exitCode"] = exit_code
    error_number = result.get("errno")
    if type(error_number) is int and error_number in {1, 2, 5, 12, 13, 24, 28, 30}:
        diagnostic["errno"] = error_number
    operation = result.get("operation")
    if isinstance(operation, str) and operation in {"cpu_detection", "library_discovery", "process_creation", "process_execution", "cache_lock", "filesystem"}:
        diagnostic["operation"] = operation
    return diagnostic


class OCRMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: Literal["pypdf", "paddleocr-ppocrv6-small-layout"]
    confidence: float = Field(ge=0.0, le=1.0)
    page_count: int = Field(alias="pageCount", ge=1, le=1)
    image_pixels: int = Field(alias="imagePixels", ge=0, le=4_000_000)
    line_count: int = Field(alias="lineCount", ge=0, le=MAX_OUTPUT_LAYOUT_LINES)


class OCRMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_init_ms: int = Field(alias="modelInitMs", ge=0, le=MAX_WORKER_JOB_SECONDS * 1000)
    processing_ms: int = Field(alias="processingMs", ge=0, le=MAX_WORKER_JOB_SECONDS * 1000)
    ocr_render_ms: int = Field(alias="ocrRenderMs", ge=0, le=MAX_WORKER_JOB_SECONDS * 1000)
    peak_rss_bytes: int = Field(alias="peakRssBytes", ge=0, le=2 * 1024 * 1024 * 1024)
    wall_clock_ms: int = Field(alias="wallClockMs", ge=0, le=(MAX_WORKER_JOB_SECONDS + 5) * 1000)


class OCRLayoutLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=512)
    page: int = Field(ge=1, le=1)
    x0: float = Field(ge=0.0, le=1.0)
    y0: float = Field(ge=0.0, le=1.0)
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)


class OCRWorkerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(alias="workspaceId", min_length=36, max_length=36)
    source_sha256: str = Field(alias="sourceSha256", pattern="^[a-f0-9]{64}$")
    source_size_bytes: int = Field(alias="sourceSizeBytes", gt=0, le=MAX_OCR_INPUT_BYTES)
    detected_content_type: str = Field(alias="detectedContentType", min_length=1, max_length=120)
    content_validated: bool = Field(alias="contentValidated")
    format_validation_status: str = Field(alias="formatValidationStatus", pattern="^valid$")
    text: str = Field(max_length=MAX_OUTPUT_TEXT_CHARS)
    layout_lines: list[OCRLayoutLine] = Field(alias="layoutLines", max_length=MAX_OUTPUT_LAYOUT_LINES)
    bank_statement: dict[str, Any] = Field(alias="bankStatement")
    ocr: OCRMetadata
    metrics: OCRMetrics

    @model_validator(mode="after")
    def validate_layout_count(self):
        if self.ocr.line_count != len(self.layout_lines):
            raise ValueError("OCR line count does not match the bounded layout output")
        return self


class OCRWorkerResult(OCRWorkerOutput):
    schema_version: Literal[1] = Field(alias="schemaVersion")
    job_id: str = Field(alias="jobId", min_length=36, max_length=36)
    document_id: str = Field(alias="documentId", min_length=36, max_length=36)
    version_id: str = Field(alias="versionId", min_length=36, max_length=36)


class TransientReceiptResult(OCRWorkerOutput):
    schema_version: Literal[2] = Field(alias="schemaVersion")
    request_id: str = Field(alias="requestId", min_length=36, max_length=36)


def _grant_secret() -> str:
    return _signing_secret or os.getenv("OCR_WORKER_GRANT_SECRET", "")


def _configured_timeout() -> int:
    try:
        configured = int(os.getenv("OCR_WORKER_JOB_TIMEOUT_SECONDS", str(MAX_WORKER_JOB_SECONDS)))
    except ValueError as error:
        raise RuntimeError("OCR_WORKER_JOB_TIMEOUT_SECONDS must be an integer") from error
    if not 1 <= configured <= MAX_WORKER_JOB_SECONDS:
        raise RuntimeError(f"OCR worker timeout must be between 1 and {MAX_WORKER_JOB_SECONDS} seconds")
    return configured


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from .isolation import enforce_worker_runtime

    global _signing_secret, _runtime_ready
    if len(_grant_secret().encode("utf-8")) < 32:
        raise RuntimeError("OCR_WORKER_GRANT_SECRET must contain at least 32 bytes")
    _configured_timeout()
    enforce_worker_runtime()
    # spawn starts a clean interpreter: the child inherits neither this memory
    # nor the grant credential, which is removed from the inherited environment.
    _signing_secret = _grant_secret()
    os.environ.pop("OCR_WORKER_GRANT_SECRET", None)
    _runtime_ready = True
    try:
        yield
    finally:
        _runtime_ready = False
        _signing_secret = ""


app = FastAPI(
    title="Ledgerical OCR Worker",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


def _child_process(send_connection, body: bytes, claims: dict, scratch_dir: str) -> None:
    from .processor import WorkerExecutionError, WorkerFormatError

    try:
        from .isolation import prepare_parser_sandbox

        os.environ["HOME"] = scratch_dir
        os.environ["TMPDIR"] = scratch_dir
        os.environ["XDG_CACHE_HOME"] = scratch_dir
        os.environ["XDG_CONFIG_HOME"] = scratch_dir
        # Model weights are baked into the image; runtime network probes and
        # downloads have no place in an untrusted document job.
        os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
        os.environ["HF_HUB_OFFLINE"] = "1"
        tempfile.tempdir = scratch_dir
        os.chdir(scratch_dir)
        # Parser/native-library output must not become document-bearing logs.
        null_output = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null_output, 1)
        os.dup2(null_output, 2)
        if null_output > 2:
            os.close(null_output)
        prepare_parser_sandbox()
        from .processor import process_ocr_bytes

        result = process_ocr_bytes(body, claims, scratch_dir)
        try:
            import resource

            peak_rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)
        except Exception:
            peak_rss = 0
        result["metrics"]["peakRssBytes"] = peak_rss
        message = {"outcome": "ok", "result": result}
    except WorkerFormatError as error:
        message = {"outcome": "format_error", "statusCode": error.status_code, "code": error.code, "message": str(error)}
    except WorkerExecutionError as error:
        message = {"outcome": "worker_error", "stage": error.stage, "errorType": error.error_type,
                   "errno": error.error_number, "operation": error.operation}
    except BaseException as error:
        # Never forward exception text, stack traces, temp paths or document data.
        message = {"outcome": "worker_error", "stage": "parser_startup", "errorType": type(error).__name__}
    try:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= MAX_CHILD_IPC_BYTES:
            send_connection.send_bytes(encoded)
    except Exception:
        pass
    finally:
        send_connection.close()


def _run_isolated_child(body: bytes, grant: OCRInputGrant, scratch_dir: str, timeout_seconds: int):
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_child_process,
        args=(send_connection, body, grant.model_dump(by_alias=True, mode="json"), scratch_dir),
        daemon=True,
    )
    started = time.monotonic()
    process_started = False
    received: list[dict] = []
    receive_errors: list[BaseException] = []

    def receive_message() -> None:
        try:
            encoded = receive_connection.recv_bytes(maxlength=MAX_CHILD_IPC_BYTES)
            message = json.loads(encoded.decode("utf-8"))
            if not isinstance(message, dict):
                raise ValueError("OCR child IPC payload must be a JSON object")
            received.append(message)
        except BaseException as error:
            receive_errors.append(error)

    try:
        process.start()
        process_started = True
        send_connection.close()
        receiver = threading.Thread(target=receive_message, name="ocr-child-result", daemon=True)
        receiver.start()
        remaining = max(0.0, timeout_seconds - (time.monotonic() - started))
        process.join(remaining)
        if process.is_alive():
            process.terminate()
            process.join(1)
            if process.is_alive():
                process.kill()
                process.join(1)
            receiver.join(1)
            raise TimeoutError("OCR worker job exceeded its hard wall-clock limit")
        receiver.join(1)
        if receiver.is_alive():
            raise RuntimeError("OCR child result transfer did not finish")
        if process.exitcode != 0:
            return {"outcome": "worker_error", "stage": "child_exit", "errorType": "ProcessExit", "exitCode": process.exitcode}, max(0, int((time.monotonic() - started) * 1000))
        if receive_errors:
            raise RuntimeError("OCR child did not return a valid result") from receive_errors[0]
        if not received:
            raise RuntimeError("OCR child returned no result")
        return received[0], max(0, int((time.monotonic() - started) * 1000))
    finally:
        send_connection.close()
        receive_connection.close()
        if process_started:
            if process.is_alive():
                process.kill()
                process.join(1)
            if not process.is_alive():
                process.close()


async def _read_bounded_body(request: Request, expected_size: int) -> bytes:
    content_length = request.headers.get("content-length", "")
    try:
        declared_length = int(content_length)
    except ValueError as error:
        raise HTTPException(status_code=411, detail={"error": "CONTENT_LENGTH_REQUIRED"}) from error
    if declared_length != expected_size or declared_length > MAX_OCR_INPUT_BYTES:
        raise HTTPException(status_code=413, detail={"error": "OCR_INPUT_LIMIT", "message": "OCR body size does not match its signed grant"})

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > expected_size or len(body) + len(chunk) > MAX_OCR_INPUT_BYTES:
            raise HTTPException(status_code=413, detail={"error": "OCR_INPUT_LIMIT", "message": "OCR body exceeds its signed limit"})
        body.extend(chunk)
    if len(body) != expected_size:
        raise HTTPException(status_code=409, detail={"error": "OCR_INPUT_SIZE_MISMATCH"})
    return bytes(body)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    if not _runtime_ready:
        raise HTTPException(status_code=503, detail={"error": "OCR_SANDBOX_UNAVAILABLE"})
    return {"status": "ok", "worker": "isolated-ocr-v1"}


@app.post("/v1/process", response_model=OCRWorkerResult | TransientReceiptResult, response_model_by_alias=True)
async def process_document(
    request: Request,
    job_grant: str = Header(default="", alias="X-Ledgerical-OCR-Job-Grant"),
):
    if not _runtime_ready:
        raise HTTPException(status_code=503, detail={"error": "OCR_SANDBOX_UNAVAILABLE"})
    try:
        grant = verify_ocr_job_grant(job_grant, secret=_grant_secret())
    except OCRJobGrantError as error:
        raise HTTPException(status_code=401, detail={"error": "INVALID_OCR_JOB_GRANT", "message": str(error)}) from error

    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/octet-stream":
        raise HTTPException(status_code=415, detail={"error": "OCR_INPUT_CONTENT_TYPE_INVALID"})

    global _active_job
    if _active_job:
        raise HTTPException(status_code=429, headers={"Retry-After": "2"}, detail={"error": "OCR_WORKER_BUSY"})
    _active_job = True
    correlation_id = str(grant.request_id if isinstance(grant, TransientReceiptGrant) else grant.job_id)
    started = time.monotonic()
    scratch_dir = ""
    try:
        try:
            body = await asyncio.wait_for(_read_bounded_body(request, grant.size_bytes), timeout=5)
        except TimeoutError as error:
            raise HTTPException(status_code=408, detail={"error": "OCR_INPUT_TIMEOUT"}) from error
        scratch_dir = tempfile.mkdtemp(prefix="ocr-job-", dir=tempfile.gettempdir())
        try:
            result, elapsed_ms = await asyncio.to_thread(
                _run_isolated_child,
                body,
                grant,
                scratch_dir,
                _configured_timeout(),
            )
        except TimeoutError as error:
            logger.warning("ocr_job_timeout job_id=%s elapsed_ms=%s", correlation_id, int((time.monotonic() - started) * 1000))
            raise HTTPException(status_code=504, detail={"error": "OCR_WORKER_TIMEOUT", "message": "OCR exceeded the worker time limit"}) from error
        except Exception as error:
            logger.error("ocr_job_runtime_failure job_id=%s error_type=%s", correlation_id, type(error).__name__)
            raise HTTPException(status_code=503, detail={"error": "OCR_WORKER_UNAVAILABLE", "message": "isolated OCR processing is temporarily unavailable"}) from error

        outcome = result.get("outcome") if isinstance(result, dict) else "worker_error"
        if outcome == "format_error":
            raise HTTPException(
                status_code=int(result.get("statusCode", 422)),
                detail={"error": result.get("code", "INVALID_DOCUMENT_FORMAT"), "message": result.get("message", "document format is invalid")},
            )
        if outcome != "ok" or not isinstance(result.get("result"), dict):
            diagnostic = safe_failure_diagnostic(result if isinstance(result, dict) else {})
            logger.error("ocr_job_child_failure job_id=%s diagnostic=%s", correlation_id, diagnostic)
            code = "OCR_MODEL_UNAVAILABLE" if diagnostic["stage"] == "model_initialization" else "OCR_WORKER_UNAVAILABLE"
            raise HTTPException(status_code=503, detail={"error": code, "message": "Receipt processing is temporarily unavailable."})

        payload = dict(result["result"])
        payload["metrics"]["wallClockMs"] = elapsed_ms
        result_type = TransientReceiptResult if isinstance(grant, TransientReceiptGrant) else OCRWorkerResult
        validated = result_type.model_validate(payload)
        logger.info(
            "ocr_job_complete job_id=%s source_size_bytes=%s model_init_ms=%s processing_ms=%s ocr_render_ms=%s peak_rss_bytes=%s wall_clock_ms=%s",
            correlation_id,
            validated.source_size_bytes,
            validated.metrics.model_init_ms,
            validated.metrics.processing_ms,
            validated.metrics.ocr_render_ms,
            validated.metrics.peak_rss_bytes,
            validated.metrics.wall_clock_ms,
        )
        return validated
    finally:
        if scratch_dir:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        _active_job = False
