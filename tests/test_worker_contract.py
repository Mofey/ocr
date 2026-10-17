from __future__ import annotations

import asyncio
import binascii
import hashlib
import json
import struct
import sys
import time
import types
from io import BytesIO
from pathlib import Path
import zlib

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter

from ocr_worker import app as worker_app
from ocr_worker.grant import OCRJobGrantError, encode_test_grant, verify_ocr_job_grant
from ocr_worker import ocr
from ocr_worker.processor import WorkerFormatError, _bounded_pdf_render_scale, _validate_image, process_ocr_bytes


SECRET = "0123456789abcdef0123456789abcdef"
NOW = int(time.time())
WORKSPACE = "22222222-2222-4222-8222-222222222222"
DOCUMENT = "33333333-3333-4333-8333-333333333333"
VERSION = "44444444-4444-4444-8444-444444444444"
JOB = "11111111-1111-4111-8111-111111111111"
OBJECT_KEY = f"immutable/workspaces/{WORKSPACE}/documents/{DOCUMENT}/versions/{VERSION}"


def _claims(content: bytes = b"abc", filename: str = "invoice.pdf", content_type: str = "application/pdf") -> dict:
    return {
        "schemaVersion": 1,
        "audience": "document-ocr-worker",
        "jobId": JOB,
        "workspaceId": WORKSPACE,
        "documentId": DOCUMENT,
        "versionId": VERSION,
        "objectKey": OBJECT_KEY,
        "filename": filename,
        "contentType": content_type,
        "sizeBytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "issuedAt": NOW,
        "expiresAt": NOW + 120,
    }


def test_grant_verification_binds_workspace_version_hash_and_expiry() -> None:
    payload = _claims()
    token = encode_test_grant(payload, SECRET)
    grant = verify_ocr_job_grant(token, SECRET, now=NOW + 1)
    assert str(grant.job_id) == JOB
    assert str(grant.workspace_id) == WORKSPACE
    assert grant.object_key == OBJECT_KEY
    assert grant.sha256 == hashlib.sha256(b"abc").hexdigest()

    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(OCRJobGrantError):
        verify_ocr_job_grant(tampered, SECRET, now=NOW + 1)
    with pytest.raises(OCRJobGrantError):
        verify_ocr_job_grant(token, SECRET, now=NOW + 120)


def test_worker_endpoint_returns_bounded_result_bound_to_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_WORKER_GRANT_SECRET", SECRET)
    content = b"abc"
    grant = encode_test_grant(_claims(content), SECRET)
    result = {
        "schemaVersion": 1,
        "jobId": JOB,
        "workspaceId": WORKSPACE,
        "documentId": DOCUMENT,
        "versionId": VERSION,
        "sourceSha256": hashlib.sha256(content).hexdigest(),
        "sourceSizeBytes": len(content),
        "detectedContentType": "application/pdf",
        "contentValidated": True,
        "formatValidationStatus": "valid",
        "text": "Invoice number INV-42 Total NGN 100.00",
        "layoutLines": [],
        "bankStatement": {"rows": []},
        "ocr": {"engine": "pypdf", "confidence": 1.0, "pageCount": 1, "imagePixels": 0, "lineCount": 0},
        "metrics": {"modelInitMs": 0, "processingMs": 0, "ocrRenderMs": 0, "peakRssBytes": 1, "wallClockMs": 1},
    }
    monkeypatch.setattr(worker_app, "_run_isolated_child", lambda *_args: ({"outcome": "ok", "result": result}, 1))

    async def call_worker():
        transport = httpx.ASGITransport(app=worker_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
            return await client.post(
                "/v1/process",
                content=content,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Ledgerical-OCR-Job-Grant": grant,
                },
            )

    response = asyncio.run(call_worker())
    assert response.status_code == 200
    assert response.json()["sourceSha256"] == result["sourceSha256"]
    assert response.json()["jobId"] == JOB


def test_worker_rejects_missing_grant_and_oversized_or_mismatched_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_WORKER_GRANT_SECRET", SECRET)
    good_grant = encode_test_grant(_claims(b"abc"), SECRET)

    async def call_worker(headers: dict[str, str], content: bytes):
        transport = httpx.ASGITransport(app=worker_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
            return await client.post("/v1/process", content=content, headers=headers)

    missing = asyncio.run(call_worker({"Content-Type": "application/octet-stream"}, b"abc"))
    assert missing.status_code == 401

    mismatch = asyncio.run(
        call_worker(
            {"Content-Type": "application/octet-stream", "X-Ledgerical-OCR-Job-Grant": good_grant},
            b"abcd",
        )
    )
    assert mismatch.status_code == 413

    oversized = asyncio.run(
        call_worker(
            {"Content-Type": "application/octet-stream", "X-Ledgerical-OCR-Job-Grant": good_grant},
            b"x" * (2 * 1024 * 1024 + 1),
        )
    )
    assert oversized.status_code == 413


def test_parser_child_rejects_hash_mismatch_before_loading_paddle(tmp_path: Path) -> None:
    claims = _claims(b"abc")
    claims["sha256"] = "0" * 64
    with pytest.raises(WorkerFormatError) as error:
        process_ocr_bytes(b"abc", claims, str(tmp_path))
    assert error.value.code == "SOURCE_HASH_MISMATCH"


def test_pdf_render_scale_is_bounded_by_edge_and_pixel_limits() -> None:
    scale = _bounded_pdf_render_scale(14_400, 14_400)
    assert 14_400 * scale <= 2_000
    assert (14_400 * scale) ** 2 <= 4_000_000

    with pytest.raises(WorkerFormatError):
        _bounded_pdf_render_scale(float("inf"), 792)
    with pytest.raises(WorkerFormatError):
        _bounded_pdf_render_scale(14_401, 792)


def test_compressed_image_over_pixel_limit_is_rejected_before_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    def png_chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)

    # The tiny body declares 36 million decoded pixels. Pillow must reject its
    # dimensions while opening the header, before image.verify/load can decode it.
    content = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 6_000, 6_000, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(b""))
        + png_chunk(b"IEND", b"")
    )
    assert len(content) < 2 * 1024 * 1024

    from PIL import Image

    monkeypatch.setattr(Image.Image, "verify", lambda _self: pytest.fail("oversized image was decoded"))
    with pytest.raises(WorkerFormatError) as error:
        _validate_image(content, "oversized.png", "image/png")
    assert error.value.code == "INVALID_DOCUMENT_FORMAT"


def test_two_page_pdf_is_rejected_before_ocr_model_initialization(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    content = output.getvalue()
    monkeypatch.setattr(ocr, "_get_ocr_reader", lambda: pytest.fail("OCR model loaded for a disallowed PDF"))

    with pytest.raises(WorkerFormatError) as error:
        process_ocr_bytes(content, _claims(content), str(tmp_path))
    assert error.value.code == "INVALID_DOCUMENT_FORMAT"


def test_worker_renders_pdf_page_to_bounded_image_before_ocr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=14_400, height=14_400)
    output = BytesIO()
    writer.write(output)
    content = output.getvalue()
    observed: dict[str, int] = {}

    class FakeBitmap:
        def __init__(self, width: int, height: int):
            self.image = Image.new("RGB", (width, height), "white")

        def to_pil(self):
            return self.image.copy()

        def close(self):
            self.image.close()

    class FakePage:
        def get_size(self):
            return 14_400, 14_400

        def render(self, *, scale: float, rotation: int):
            assert rotation == 0
            width = round(14_400 * scale)
            height = round(14_400 * scale)
            observed["pixels"] = width * height
            return FakeBitmap(width, height)

        def close(self):
            pass

    class FakeDocument:
        def __init__(self, _content: bytes):
            pass

        def __len__(self):
            return 1

        def __getitem__(self, index: int):
            assert index == 0
            return FakePage()

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "pypdfium2", types.SimpleNamespace(PdfDocument=FakeDocument))
    monkeypatch.setattr(ocr, "_get_ocr_reader", lambda: object())

    async def fake_image_ocr(image_bytes: bytes):
        assert image_bytes.startswith(b"\x89PNG")
        return "Large page", 0.9, [{"text": "Large page", "page": 1, "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0, "confidence": 0.9}]

    monkeypatch.setattr(ocr, "ocr_image_layout", fake_image_ocr)
    result = process_ocr_bytes(content, _claims(content), str(tmp_path))
    assert observed["pixels"] <= 4_000_000
    assert result["ocr"]["imagePixels"] == observed["pixels"]
    assert result["metrics"]["ocrRenderMs"] >= 0


def test_grant_rejects_non_immutable_key_and_legacy_or_macro_types() -> None:
    payload = _claims()
    payload["objectKey"] = f"quarantine/workspaces/{WORKSPACE}/uploads/{JOB}"
    token = encode_test_grant(payload, SECRET)
    with pytest.raises(OCRJobGrantError):
        verify_ocr_job_grant(token, SECRET, now=NOW + 1)

    legacy = _claims(filename="report.docx", content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    token = encode_test_grant(legacy, SECRET)
    with pytest.raises(OCRJobGrantError):
        verify_ocr_job_grant(token, SECRET, now=NOW + 1)
