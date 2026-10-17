"""Shared local PaddleOCR pipeline for receipts and document processing."""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
import os
import tempfile
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

PP_OCR_DETECTION_MODEL_NAME = "PP-OCRv6_small_det"
PP_OCR_RECOGNITION_MODEL_NAME = "PP-OCRv6_small_rec"
PP_OCR_TEXTLINE_ORIENTATION_MODEL_NAME = "PP-LCNet_x0_25_textline_ori"
PP_OCR_MODEL_DISPLAY_NAME = "PaddleOCR-PP-OCRv6-small"
PP_OCR_PDF_MODEL_DISPLAY_NAME = "PaddleOCR-PP-OCRv6-small-PDF"
PP_OCR_IMAGE_ENGINE = "paddleocr-ppocrv6-small"
PP_OCR_LAYOUT_ENGINE = "paddleocr-ppocrv6-small-layout"
_MIN_TEXT_CONFIDENCE = 0.40

_ocr_reader = None
_ocr_lock = threading.Lock()


@dataclass(frozen=True)
class OCRLineEvidence:
    """Transient OCR/native-text line with confidence and a stable evidence hash.

    Remittance extraction uses the line text only in memory. Persisted
    intelligence stores ``evidence_hash`` plus structured extracted fields, not
    the complete OCR body.
    """

    text: str
    confidence: float
    page: int = 1
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 1.0
    y1: float = 1.0
    source: str = "native_text"
    evidence_hash: str = ""


def _line_value(line: Any, key: str, default: Any) -> Any:
    if isinstance(line, Mapping):
        return line.get(key, default)
    return getattr(line, key, default)


def _bounded_confidence(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(numeric):
        return 0.0
    return max(0.0, min(1.0, numeric))


def _line_evidence_hash(
    *,
    page: int,
    text: str,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    source: str,
) -> str:
    # Include location/source so repeated identical text on the same page still
    # has distinct evidence provenance without persisting the raw line itself.
    canonical = "|".join(
        (
            str(page),
            source,
            f"{x0:.6f}",
            f"{y0:.6f}",
            f"{x1:.6f}",
            f"{y1:.6f}",
            " ".join(text.split()).strip(),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalise_ocr_lines(
    text: str,
    *,
    ocr_confidence: float = 1.0,
    layout_lines: Iterable[Any] | None = None,
) -> list[OCRLineEvidence]:
    """Build confidence-carrying lines for deterministic field extraction.

    ``layout_lines`` accepts the dictionaries returned by ``ocr_*_layout`` or
    layout-like objects used by existing document AI. If no layout is
    available, native/extracted text is split into lines and receives the
    caller-supplied aggregate confidence.
    """

    default_confidence = _bounded_confidence(ocr_confidence)
    rows: list[OCRLineEvidence] = []
    if layout_lines is not None:
        for raw in layout_lines:
            raw_text = " ".join(str(_line_value(raw, "text", "") or "").split()).strip()
            if not raw_text:
                continue
            try:
                page = max(1, int(_line_value(raw, "page", 1) or 1))
            except (TypeError, ValueError):
                page = 1
            raw_confidence = _line_value(raw, "confidence", None)
            confidence = default_confidence if raw_confidence is None else _bounded_confidence(raw_confidence)
            confidence = min(confidence, default_confidence)
            try:
                x0 = float(_line_value(raw, "x0", 0.0) or 0.0)
                y0 = float(_line_value(raw, "y0", 0.0) or 0.0)
                x1 = float(_line_value(raw, "x1", 1.0) or 1.0)
                y1 = float(_line_value(raw, "y1", 1.0) or 1.0)
            except (TypeError, ValueError):
                x0, y0, x1, y1 = 0.0, 0.0, 1.0, 1.0
            rows.append(
                OCRLineEvidence(
                    text=raw_text,
                    confidence=confidence,
                    page=page,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                    source="ocr_line",
                    evidence_hash=_line_evidence_hash(
                        page=page,
                        text=raw_text,
                        x0=x0,
                        y0=y0,
                        x1=x1,
                        y1=y1,
                        source="ocr_line",
                    ),
                )
            )
    if rows:
        return sorted(rows, key=lambda row: (row.page, round(row.y0, 4), row.x0))

    fallback_confidence = default_confidence
    for index, raw_text in enumerate(text.splitlines()):
        cleaned = " ".join(raw_text.split()).strip()
        if not cleaned:
            continue
        rows.append(
            OCRLineEvidence(
                text=cleaned,
                confidence=fallback_confidence,
                page=1,
                y0=float(index),
                y1=float(index) + 0.8,
                source="native_text",
                evidence_hash=_line_evidence_hash(
                    page=1,
                    text=cleaned,
                    x0=0.0,
                    y0=float(index),
                    x1=1.0,
                    y1=float(index) + 0.8,
                    source="native_text",
                ),
            )
        )
    return rows


def _build_ocr_reader():
    from paddleocr import PaddleOCR  # type: ignore[import]

    # PaddleOCR 3.x delegates model construction to PaddleX. PaddleX logs
    # normal model/cache status at INFO (for example, "Creating model" and
    # "Model files already exist"). Keep production startup logs focused on
    # actionable warnings/errors without hiding failures.
    logging.getLogger("paddlex").setLevel(logging.WARNING)

    return PaddleOCR(
        text_detection_model_name=PP_OCR_DETECTION_MODEL_NAME,
        text_recognition_model_name=PP_OCR_RECOGNITION_MODEL_NAME,
        textline_orientation_model_name=PP_OCR_TEXTLINE_ORIENTATION_MODEL_NAME,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=True,
        device="cpu",
        text_det_thresh=0.3,
        text_det_box_thresh=0.5,
        text_det_unclip_ratio=1.6,
        text_rec_score_thresh=_MIN_TEXT_CONFIDENCE,
        text_recognition_batch_size=6,
        textline_orientation_batch_size=6,
    )


def _get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        with _ocr_lock:
            if _ocr_reader is None:
                _ocr_reader = _build_ocr_reader()
    return _ocr_reader


def _get_pdf_ocr_reader(_max_pages: int):
    return _get_ocr_reader()


def _preprocess_receipt(file_bytes: bytes) -> bytes:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps  # type: ignore[import]

    img = Image.open(io.BytesIO(file_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    width, height = img.size
    long_side = max(width, height)
    if long_side <= 0:
        raise ValueError("image has invalid dimensions")
    if long_side < 1500:
        scale = 1500 / long_side
        img = img.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS)

    gray = img.convert("L")
    histogram = gray.histogram()
    total = sum(histogram) or 1
    mean_luminance = sum(index * count for index, count in enumerate(histogram)) / total
    contrast_factor = 2.2 if mean_luminance > 210 else 1.8 if mean_luminance > 170 else 1.4
    gray = ImageEnhance.Contrast(gray).enhance(contrast_factor)
    gray = gray.filter(ImageFilter.SHARPEN).filter(ImageFilter.SHARPEN)

    output = io.BytesIO()
    gray.convert("RGB").save(output, format="PNG")
    return output.getvalue()


def _run_paddle_sync(file_bytes: bytes) -> tuple[str, float]:
    import numpy as np  # type: ignore[import]
    from PIL import Image  # type: ignore[import]

    try:
        enhanced = _preprocess_receipt(file_bytes)
    except Exception:
        enhanced = file_bytes

    image = np.array(Image.open(io.BytesIO(enhanced)).convert("RGB"))
    results = _run_ocr_prediction(_get_ocr_reader(), image)
    return _collect_ocr_lines(results)


async def warm_receipt_ocr() -> None:
    """Initialize the shared receipt OCR reader before receipt traffic is accepted."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _get_ocr_reader)


async def ocr_image(file_bytes: bytes) -> tuple[str, float]:
    """Run local OCR without blocking the FastAPI event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_paddle_sync, file_bytes)


def _run_ocr_prediction(reader, input_data):
    predict = getattr(reader, "predict", None)
    if callable(predict):
        return predict(input_data)
    legacy_ocr = getattr(reader, "ocr", None)
    if callable(legacy_ocr):
        try:
            return legacy_ocr(input_data, cls=True)
        except TypeError:
            return legacy_ocr(input_data)
    raise AttributeError("PaddleOCR reader has no predict or ocr method")


def _as_list(value) -> list:
    if value is None:
        return []
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            value = tolist()
        except Exception:
            pass
    if isinstance(value, (str, bytes, bytearray)):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def _result_payload(result) -> dict | None:
    payload = result
    if not isinstance(payload, dict):
        json_payload = getattr(result, "json", None)
        if isinstance(json_payload, dict):
            payload = json_payload
        elif callable(json_payload):
            try:
                payload = json_payload()
            except Exception:
                return None
    if isinstance(payload, dict) and isinstance(payload.get("res"), dict):
        return payload["res"]
    return payload if isinstance(payload, dict) else None


def _coerce_points(raw_box) -> list[tuple[float, float]]:
    values = _as_list(raw_box)
    if not values:
        return []
    first = values[0]
    if isinstance(first, dict):
        first = list(first.values())
    nested = not isinstance(first, (int, float, str, bytes, bytearray))
    if nested:
        points: list[tuple[float, float]] = []
        for point in values:
            coords = _as_list(point)
            if len(coords) < 2:
                continue
            try:
                points.append((float(coords[0]), float(coords[1])))
            except (TypeError, ValueError):
                continue
        return points
    if len(values) >= 4:
        try:
            x0, y0, x1, y1 = (float(values[0]), float(values[1]), float(values[2]), float(values[3]))
        except (TypeError, ValueError):
            return []
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return []


def _parse_legacy_row(row) -> tuple[list[tuple[float, float]], str, float] | None:
    if isinstance(row, (str, bytes, bytearray)):
        return None
    values = _as_list(row)
    if len(values) != 2:
        return None
    bbox, recognition = values
    rec_values = _as_list(recognition)
    if len(rec_values) < 2:
        return None
    try:
        confidence = float(rec_values[1])
    except (TypeError, ValueError):
        return None
    text = str(rec_values[0]).strip()
    return _coerce_points(bbox), text, confidence


def _iter_v3_rows(results):
    for page_index, result in enumerate(_as_list(results), start=1):
        payload = _result_payload(result)
        if not payload or "rec_texts" not in payload:
            continue
        texts = _as_list(payload.get("rec_texts"))
        scores = _as_list(payload.get("rec_scores"))
        polys = _as_list(payload.get("rec_polys"))
        boxes = _as_list(payload.get("rec_boxes"))
        for index, raw_text in enumerate(texts):
            text = str(raw_text).strip()
            try:
                confidence = float(scores[index]) if index < len(scores) else 0.0
            except (TypeError, ValueError):
                confidence = 0.0
            raw_box = (
                polys[index]
                if index < len(polys)
                else boxes[index] if index < len(boxes) else None
            )
            yield page_index, index, _coerce_points(raw_box), text, confidence


def _iter_legacy_rows(results):
    values = _as_list(results)
    if _parse_legacy_row(values) is not None:
        values = [values]
    pages = [values] if values and _parse_legacy_row(values[0]) is not None else values
    for page_index, page in enumerate(pages, start=1):
        rows = [page] if _parse_legacy_row(page) is not None else _as_list(page)
        for index, row in enumerate(rows):
            parsed = _parse_legacy_row(row)
            if parsed is None:
                continue
            points, text, confidence = parsed
            yield page_index, index, points, text, confidence


def _iter_ocr_rows(results):
    v3_rows = list(_iter_v3_rows(results))
    if v3_rows:
        yield from v3_rows
        return
    yield from _iter_legacy_rows(results)


def _row_sort_key(row) -> tuple[float, float, float, int]:
    page_index, row_index, points, _text, _confidence = row
    if not points:
        return float(page_index), float(row_index), 0.0, row_index
    return (
        float(page_index),
        min(point[1] for point in points),
        min(point[0] for point in points),
        row_index,
    )


def _collect_ocr_lines(results) -> tuple[str, float]:
    """Flatten PaddleOCR v3/v2 image or PDF results without trusting result shape."""
    lines: list[str] = []
    confidences: list[float] = []
    if not results:
        return "", 0.0

    for _page_index, _row_index, _points, text, confidence in sorted(
        _iter_ocr_rows(results),
        key=_row_sort_key,
    ):
        if confidence > _MIN_TEXT_CONFIDENCE and text:
            lines.append(text)
            confidences.append(confidence)
    average = sum(confidences) / len(confidences) if confidences else 0.0
    return "\n".join(lines), average


def _bounded_pdf_path(path: str, max_pages: int) -> tuple[str, str | None]:
    max_pages = max(1, min(int(max_pages or 1), 100))
    try:
        from pypdf import PdfReader, PdfWriter  # type: ignore[import]

        reader = PdfReader(path)
        if len(reader.pages) <= max_pages:
            return path, None
        writer = PdfWriter()
        for page in reader.pages[:max_pages]:
            writer.add_page(page)
        fd, temp_path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            with open(temp_path, "wb") as output:
                writer.write(output)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            return path, None
        return temp_path, temp_path
    except Exception:
        return path, None


def _run_pdf_paddle_sync(path: str, max_pages: int) -> tuple[str, float]:
    bounded_path, cleanup_path = _bounded_pdf_path(path, max_pages)
    try:
        results = _run_ocr_prediction(_get_pdf_ocr_reader(max_pages), bounded_path)
        return _collect_ocr_lines(results)
    finally:
        if cleanup_path:
            try:
                os.unlink(cleanup_path)
            except OSError:
                pass


async def ocr_pdf(path: str, max_pages: int = 10) -> tuple[str, float]:
    """OCR at most ``max_pages`` from a PDF using the local PaddleOCR model."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_pdf_paddle_sync, path, max_pages)


def _collect_ocr_layout(results) -> tuple[str, float, list[dict]]:
    """Return OCR text plus normalized line geometry for Accounting Intelligence document AI."""
    lines: list[str] = []
    confidences: list[float] = []
    layout: list[dict] = []
    if not results:
        return "", 0.0, layout
    rows_by_page: dict[int, list[tuple[int, list[tuple[float, float]], str, float]]] = {}
    for page_index, row_index, points, text, confidence in _iter_ocr_rows(results):
        if confidence <= _MIN_TEXT_CONFIDENCE or not text or not points:
            continue
        rows_by_page.setdefault(page_index, []).append((row_index, points, text, confidence))

    for page_index in sorted(rows_by_page):
        page_rows: list[tuple[float, float, dict]] = []
        xs: list[float] = []
        ys: list[float] = []
        raw_rows = []
        for _row_index, points, text, confidence in rows_by_page[page_index]:
            x0 = min(point[0] for point in points)
            x1 = max(point[0] for point in points)
            y0 = min(point[1] for point in points)
            y1 = max(point[1] for point in points)
            xs.extend([x0, x1])
            ys.extend([y0, y1])
            raw_rows.append((x0, y0, x1, y1, text, confidence))
        width = max(xs) - min(xs) if xs else 1.0
        height = max(ys) - min(ys) if ys else 1.0
        x_min = min(xs) if xs else 0.0
        y_min = min(ys) if ys else 0.0
        for x0, y0, x1, y1, text, confidence in raw_rows:
            payload = {
                "text": text,
                "page": page_index,
                "x0": (x0 - x_min) / max(width, 1.0),
                "y0": (y0 - y_min) / max(height, 1.0),
                "x1": (x1 - x_min) / max(width, 1.0),
                "y1": (y1 - y_min) / max(height, 1.0),
                "confidence": confidence,
            }
            page_rows.append((payload["y0"], payload["x0"], payload))
        for _, _, payload in sorted(page_rows, key=lambda item: (round(item[0], 3), item[1])):
            lines.append(payload["text"])
            confidences.append(float(payload["confidence"]))
            layout.append(payload)
    average = sum(confidences) / len(confidences) if confidences else 0.0
    return "\n".join(lines), average, layout


def _run_paddle_layout_sync(file_bytes: bytes) -> tuple[str, float, list[dict]]:
    import numpy as np  # type: ignore[import]
    from PIL import Image  # type: ignore[import]

    try:
        enhanced = _preprocess_receipt(file_bytes)
    except Exception:
        enhanced = file_bytes
    image = np.array(Image.open(io.BytesIO(enhanced)).convert("RGB"))
    results = _run_ocr_prediction(_get_ocr_reader(), image)
    return _collect_ocr_layout(results)


async def ocr_image_layout(file_bytes: bytes) -> tuple[str, float, list[dict]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_paddle_layout_sync, file_bytes)


def _run_pdf_paddle_layout_sync(path: str, max_pages: int) -> tuple[str, float, list[dict]]:
    bounded_path, cleanup_path = _bounded_pdf_path(path, max_pages)
    try:
        results = _run_ocr_prediction(_get_pdf_ocr_reader(max_pages), bounded_path)
        return _collect_ocr_layout(results)
    finally:
        if cleanup_path:
            try:
                os.unlink(cleanup_path)
            except OSError:
                pass


async def ocr_pdf_layout(path: str, max_pages: int = 10) -> tuple[str, float, list[dict]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_pdf_paddle_layout_sync, path, max_pages)
