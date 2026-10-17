"""Untrusted image/PDF processing code. Import this module only in the worker job child."""
from __future__ import annotations

import asyncio
import io
import math
import os
import warnings
from pathlib import Path
from typing import Any


MAX_OCR_INPUT_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000
MAX_PDF_RENDER_EDGE = 2_000
MAX_TEXT_CHARS = 200_000
MAX_LAYOUT_LINES = 500
MAX_LAYOUT_LINE_CHARS = 512

_FORMATS = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}


class WorkerFormatError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _bounded_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, number)) if math.isfinite(number) else 0.0


def _bounded_layout(raw_lines: list[dict]) -> list[dict]:
    output: list[dict] = []
    for row in raw_lines[:MAX_LAYOUT_LINES]:
        text = " ".join(str(row.get("text") or "").split()).strip()[:MAX_LAYOUT_LINE_CHARS]
        if not text:
            continue
        try:
            x0, y0, x1, y1 = (float(row.get(key, 0.0)) for key in ("x0", "y0", "x1", "y1"))
            page = max(1, min(int(row.get("page", 1)), 1))
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) for value in (x0, y0, x1, y1)):
            continue
        if not (0.0 <= x0 <= x1 <= 1.0 and 0.0 <= y0 <= y1 <= 1.0):
            continue
        output.append(
            {
                "text": text,
                "page": page,
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "confidence": _bounded_confidence(row.get("confidence", 0.0)),
            }
        )
    return output


def _validate_pdf(content: bytes, filename: str, content_type: str) -> tuple[str, int]:
    if not filename.lower().endswith(".pdf") or content_type != "application/pdf":
        raise WorkerFormatError("UNSUPPORTED_DOCUMENT_FORMAT", "only declared PDF and image OCR inputs are accepted", 415)
    if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-4096:]:
        raise WorkerFormatError("INVALID_DOCUMENT_FORMAT", "PDF signature or end marker is invalid")

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            raise WorkerFormatError("INVALID_DOCUMENT_FORMAT", "encrypted PDFs are not supported for OCR")
        page_count = len(reader.pages)
        if page_count != 1:
            raise WorkerFormatError("INVALID_DOCUMENT_FORMAT", "OCR PDFs must contain exactly one page")
        page_text = (reader.pages[0].extract_text() or "")[:MAX_TEXT_CHARS]
    except WorkerFormatError:
        raise
    except Exception as error:
        raise WorkerFormatError("INVALID_DOCUMENT_FORMAT", "PDF structure is invalid or unsupported") from error

    return page_text, page_count


def _validate_image(content: bytes, filename: str, content_type: str) -> tuple[int, int]:
    extension = Path(filename).suffix.lower()
    expected_type = _FORMATS.get(extension)
    if extension not in _IMAGE_FORMATS or expected_type != content_type:
        raise WorkerFormatError("UNSUPPORTED_DOCUMENT_FORMAT", "only declared PDF and image OCR inputs are accepted", 415)

    try:
        from PIL import Image, ImageFile

        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(content))
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise WorkerFormatError("INVALID_DOCUMENT_FORMAT", "decoded image dimensions exceed the OCR limit")
            if image.format != _IMAGE_FORMATS[extension]:
                raise WorkerFormatError("INVALID_DOCUMENT_FORMAT", "image signature does not match its extension")
            image.verify()
        return width, height
    except WorkerFormatError:
        raise
    except Exception as error:
        raise WorkerFormatError("INVALID_DOCUMENT_FORMAT", "image structure is invalid or unsupported") from error


def _bounded_pdf_render_scale(width: float, height: float) -> float:
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise WorkerFormatError("INVALID_DOCUMENT_FORMAT", "PDF page dimensions are invalid")
    if width > 14_400 or height > 14_400:
        raise WorkerFormatError("INVALID_DOCUMENT_FORMAT", "PDF page dimensions exceed the supported limit")
    max_edge_scale = MAX_PDF_RENDER_EDGE / max(width, height)
    max_area_scale = math.sqrt(MAX_IMAGE_PIXELS / (width * height))
    return min(max_edge_scale, max_area_scale) * 0.999


def _render_pdf_page_bounded(content: bytes) -> tuple[bytes, int]:
    """Render one PDF page with explicit edge and pixel-count ceilings."""
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(content)
        try:
            if len(document) != 1:
                raise WorkerFormatError("INVALID_DOCUMENT_FORMAT", "OCR PDFs must contain exactly one page")
            page = document[0]
            try:
                width, height = page.get_size()
                scale = _bounded_pdf_render_scale(float(width), float(height))
                bitmap = page.render(scale=scale, rotation=0)
                try:
                    source_image = bitmap.to_pil()
                    try:
                        image = source_image.convert("RGB")
                        try:
                            pixels = image.width * image.height
                            if image.width <= 0 or image.height <= 0 or pixels > MAX_IMAGE_PIXELS:
                                raise WorkerFormatError("INVALID_DOCUMENT_FORMAT", "rendered PDF page exceeds the OCR pixel limit")
                            output = io.BytesIO()
                            image.save(output, format="PNG")
                            return output.getvalue(), pixels
                        finally:
                            image.close()
                    finally:
                        source_image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            document.close()
    except WorkerFormatError:
        raise
    except Exception as error:
        raise WorkerFormatError("INVALID_DOCUMENT_FORMAT", "PDF page could not be rendered within the OCR limits") from error


def process_ocr_bytes(content: bytes, grant: dict, scratch_dir: str) -> dict:
    """Validate and extract at most one bounded OCR job in a restricted child process."""
    import time

    processing_started = time.monotonic()
    ocr_render_ms = 0
    if not content or len(content) != int(grant["sizeBytes"]) or len(content) > MAX_OCR_INPUT_BYTES:
        raise WorkerFormatError("INVALID_DOCUMENT_FORMAT", "OCR input size does not match its signed grant", 409)

    import hashlib

    digest = hashlib.sha256(content).hexdigest()
    if digest != grant["sha256"]:
        raise WorkerFormatError("SOURCE_HASH_MISMATCH", "OCR input does not match its signed source digest", 409)

    filename = str(grant["filename"])
    content_type = str(grant["contentType"])
    extension = Path(filename).suffix.lower()
    if _FORMATS.get(extension) != content_type:
        raise WorkerFormatError("UNSUPPORTED_DOCUMENT_FORMAT", "file extension and declared type do not match", 415)

    if extension == ".pdf":
        text, page_count = _validate_pdf(content, filename, content_type)
        pixels = 0
        engine = "pypdf"
        confidence = 1.0 if len("".join(text.split())) >= 64 else 0.0
        layout: list[dict] = []
        model_init_ms = 0
        if len("".join(text.split())) < 64:
            from . import ocr

            model_init_started = time.monotonic()
            ocr._get_ocr_reader()
            model_init_ms = max(0, int((time.monotonic() - model_init_started) * 1000))
            ocr_started = time.monotonic()
            rendered_png, pixels = _render_pdf_page_bounded(content)
            text, confidence, layout = asyncio.run(ocr.ocr_image_layout(rendered_png))
            engine = ocr.PP_OCR_LAYOUT_ENGINE
            ocr_render_ms = max(0, int((time.monotonic() - ocr_started) * 1000))
    else:
        width, height = _validate_image(content, filename, content_type)
        page_count = 1
        pixels = width * height
        from . import ocr

        model_init_started = time.monotonic()
        ocr._get_ocr_reader()
        model_init_ms = max(0, int((time.monotonic() - model_init_started) * 1000))
        ocr_started = time.monotonic()
        text, confidence, layout = asyncio.run(ocr.ocr_image_layout(content))
        engine = ocr.PP_OCR_LAYOUT_ENGINE
        ocr_render_ms = max(0, int((time.monotonic() - ocr_started) * 1000))
    text = str(text or "")[:MAX_TEXT_CHARS]
    lines = _bounded_layout(layout if isinstance(layout, list) else [])
    from .bank_statement_pdf import extract_bank_statement_text

    bank_statement = extract_bank_statement_text(
        text,
        filename=filename,
        currency="XXX",
        page_count=page_count,
        ocr_engine=engine,
        ocr_confidence=confidence,
        max_text_chars=120_000,
    )
    scope = (
        {"schemaVersion": 2, "requestId": str(grant["requestId"])}
        if grant.get("schemaVersion") == 2
        else {"schemaVersion": 1, "jobId": str(grant["jobId"]), "documentId": str(grant["documentId"]), "versionId": str(grant["versionId"])}
    )
    return {
        **scope,
        "workspaceId": str(grant["workspaceId"]),
        "sourceSha256": digest,
        "sourceSizeBytes": len(content),
        "detectedContentType": content_type,
        "contentValidated": True,
        "formatValidationStatus": "valid",
        "text": text,
        "layoutLines": lines,
        "bankStatement": bank_statement,
        "ocr": {
            "engine": engine,
            "confidence": _bounded_confidence(confidence),
            "pageCount": page_count,
            "imagePixels": pixels,
            "lineCount": len(lines),
        },
        "metrics": {
            "modelInitMs": model_init_ms,
            "processingMs": max(0, int((time.monotonic() - processing_started) * 1000)),
            "ocrRenderMs": ocr_render_ms,
        },
    }
