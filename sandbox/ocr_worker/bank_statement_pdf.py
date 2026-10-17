"""Text-layer PDF extraction for bank statement import.

This module deliberately extracts embedded/selectable PDF text only. It does not OCR, classify, match, normalize, or post ledger data.
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import isclose
from typing import Any

from .ocr import ocr_pdf

DATE_PATTERN = re.compile(
    r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})\b"
)
AMOUNT_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z]{3}\s*)?[-+(]?\d[\d,]*(?:\.\d{2,4})?\)?(?:\s*(?:CR|DR))?(?![A-Za-z0-9])")
WHITESPACE_PATTERN = re.compile(r"\s+")
SPLIT_CELLS_PATTERN = re.compile(r"\t+|\s{2,}")
REFERENCE_PATTERN = re.compile(r"\b(?:REF|REFERENCE|TXN|TRN|INV|BILL|CHQ|CHEQUE)[- #:/]*[A-Z0-9][A-Z0-9/-]{1,30}\b", re.IGNORECASE)
ZERO_AMOUNT_SUMMARY_PATTERN = re.compile(r"\b(?:opening|closing|available|ledger)\s+balance\b", re.IGNORECASE)
TEXT_LAYER_THRESHOLD_COMPACT_CHARS = 64
DEFAULT_BANK_STATEMENT_OCR_TIMEOUT_SECONDS = 45.0
DEFAULT_BANK_STATEMENT_OCR_MAX_PAGES = 10

BANK_STATEMENT_LAYOUT_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "id": "debit_credit_balance",
        "amountMode": "debit_credit_balance",
        "minAmounts": 3,
        "labels": ("date", "description", "debit", "credit", "balance"),
        "baseConfidence": 0.86,
    },
    {
        "id": "single_amount_balance",
        "amountMode": "single_amount_balance",
        "minAmounts": 2,
        "labels": ("date", "description", "amount", "balance"),
        "baseConfidence": 0.78,
    },
    {
        "id": "single_amount",
        "amountMode": "single_amount",
        "minAmounts": 1,
        "labels": ("date", "description", "amount"),
        "baseConfidence": 0.62,
    },
)


@dataclass(frozen=True)
class PDFExtractionError(Exception):
    code: str
    message: str


def _clean_line(value: str) -> str:
    return WHITESPACE_PATTERN.sub(" ", value.strip())


def _parse_pdf_date(value: str) -> str | None:
    value = _clean_line(value)
    formats = (
        "%Y-%m-%d", "%Y/%m/%d",
        "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y", "%d.%m.%y",
        "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y",
        "%d %b %Y", "%d %B %Y", "%d %b %y", "%d %B %y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_pdf_amount(value: str) -> float | None:
    raw = _clean_line(value).upper()
    if not raw:
        return None
    negative = raw.endswith("DR") or (raw.startswith("(") and raw.endswith(")"))
    positive_suffix = raw.endswith("CR")
    raw = re.sub(r"\b(?:USD|GBP|EUR|NGN|CAD|AUD|JPY|ZAR|KES|GHS)\b", "", raw)
    raw = raw.replace("$", "").replace("\u00a3", "").replace("\u20ac", "").replace("\u20a6", "")
    raw = raw.replace(",", "").replace(" ", "")
    raw = raw.removesuffix("CR").removesuffix("DR")
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    if raw.startswith("+"):
        raw = raw[1:]
    try:
        amount = float(raw)
    except ValueError:
        return None
    if negative:
        amount = -abs(amount)
    elif positive_suffix:
        amount = abs(amount)
    return round(amount, 4)


def _select_pdf_row_profile(amount_count: int) -> dict[str, Any] | None:
    for profile in BANK_STATEMENT_LAYOUT_PROFILES:
        if amount_count >= int(profile["minAmounts"]):
            return profile
    return None


def _description_from_row_text(line: str, date_match: re.Match[str], amount_matches: list[re.Match[str]]) -> str:
    if not amount_matches:
        return ""
    return _clean_line(line[date_match.end():amount_matches[0].start()].strip(" -:\t"))


def _reference_from_description(description: str) -> str:
    match = REFERENCE_PATTERN.search(description)
    return _clean_line(match.group(0)) if match else ""


def _safe_page_dimension(page: Any, field: str) -> float | None:
    try:
        box = page.mediabox
        value = getattr(box, field)
        return round(float(value), 2)
    except Exception:
        return None


def _extract_possible_table_row(page_number: int, line_number: int, row_number: int, line: str) -> dict[str, Any] | None:
    date_match = DATE_PATTERN.search(line)
    if not date_match:
        return None
    amount_matches = [
        match for match in AMOUNT_PATTERN.finditer(line)
        if match.start() >= date_match.end() and ("." in match.group(0) or "," in match.group(0))
    ]
    if not amount_matches:
        return None

    cells = [cell.strip() for cell in SPLIT_CELLS_PATTERN.split(line) if cell.strip()]
    if len(cells) < 3:
        first_amount = amount_matches[0]
        description = line[date_match.end():first_amount.start()].strip(" -:\t")
        cells = [
            date_match.group(0),
            description,
            *[match.group(0) for match in amount_matches],
        ]

    amount_count = len(amount_matches)
    confidence = 0.62
    if amount_count >= 2:
        confidence += 0.12
    if len(cells) >= 4:
        confidence += 0.08
    return {
        "pageNumber": page_number,
        "rowNumber": row_number,
        "text": line,
        "lineNumber": line_number,
        "cells": cells,
        "dateText": date_match.group(0),
        "amountText": [match.group(0) for match in amount_matches],
        "confidence": round(min(confidence, 0.9), 2),
    }


def _recover_normalized_pdf_row(table_row: dict[str, Any], default_currency: str) -> dict[str, Any]:
    source_text = str(table_row.get("text") or "")
    date_match = DATE_PATTERN.search(source_text)
    amount_matches = [
        match for match in AMOUNT_PATTERN.finditer(source_text)
        if date_match and match.start() >= date_match.end() and ("." in match.group(0) or "," in match.group(0))
    ]
    source_row_number = int(table_row.get("rowNumber") or 0)
    evidence = {
        "pageNumber": int(table_row.get("pageNumber") or 0),
        "lineNumber": int(table_row.get("lineNumber") or 0),
        "sourceText": source_text,
        "cells": table_row.get("cells") or [],
    }
    base = {
        "sourceRowNumber": source_row_number,
        "pageNumber": evidence["pageNumber"],
        "lineNumber": evidence["lineNumber"],
        "postedDate": "",
        "description": "",
        "rawDescription": "",
        "amount": 0.0,
        "moneyIn": 0.0,
        "moneyOut": 0.0,
        "currency": default_currency,
        "balance": None,
        "reference": "",
        "dedupeKey": "",
        "parseConfidence": 0.0,
        "status": "parse_error",
        "errorSummary": "PDF row could not be parsed",
        "evidence": evidence,
        "rawPayload": {"tableRow": table_row},
    }
    if not date_match:
        return base
    posted_date = _parse_pdf_date(date_match.group(0))
    if not posted_date:
        return {**base, "errorSummary": "transaction date is invalid"}
    parsed_amounts = [_parse_pdf_amount(match.group(0)) for match in amount_matches]
    if any(value is None for value in parsed_amounts):
        return {**base, "postedDate": posted_date, "errorSummary": "transaction amount is invalid"}
    amounts = [float(value or 0.0) for value in parsed_amounts]
    profile = _select_pdf_row_profile(len(amounts))
    if profile is None:
        return {**base, "postedDate": posted_date, "errorSummary": "transaction amount is missing"}

    description = _description_from_row_text(source_text, date_match, amount_matches)
    if not description:
        return {**base, "postedDate": posted_date, "errorSummary": "transaction description is missing"}

    amount = 0.0
    balance: float | None = None
    status = "parsed"
    error_summary = ""
    confidence = float(profile["baseConfidence"])
    mode = str(profile["amountMode"])
    if mode == "debit_credit_balance":
        debit, credit, balance_value = abs(amounts[-3]), abs(amounts[-2]), amounts[-1]
        if debit > 0 and credit > 0:
            status = "needs_review"
            error_summary = "debit and credit are both populated"
            confidence -= 0.28
        amount = round(credit - debit, 4)
        balance = round(balance_value, 4)
    elif mode == "single_amount_balance":
        amount = round(amounts[-2], 4)
        balance = round(amounts[-1], 4)
        if amount > 0 and not re.search(r"[+()]|\bCR\b|\bDR\b", amount_matches[-2].group(0), re.IGNORECASE):
            status = "needs_review"
            error_summary = "single amount direction may need review"
            confidence -= 0.12
    else:
        amount = round(amounts[-1], 4)
        if amount > 0 and not re.search(r"[+()]|\bCR\b|\bDR\b", amount_matches[-1].group(0), re.IGNORECASE):
            status = "needs_review"
            error_summary = "single amount direction may need review"
            confidence -= 0.18

    if isclose(amount, 0.0, abs_tol=0.00005):
        status = "ignored" if ZERO_AMOUNT_SUMMARY_PATTERN.search(description) else "needs_review"
        error_summary = "zero amount statement balance line" if status == "ignored" else "transaction amount is zero"
        confidence = min(confidence, 0.55)

    money_in = round(amount, 4) if amount > 0 else 0.0
    money_out = round(abs(amount), 4) if amount < 0 else 0.0
    reference = _reference_from_description(description)
    dedupe_source = f"{posted_date}|{amount:.4f}|{default_currency}|{description}|{reference}"
    return {
        **base,
        "postedDate": posted_date,
        "description": description,
        "rawDescription": description,
        "amount": amount,
        "moneyIn": money_in,
        "moneyOut": money_out,
        "currency": default_currency,
        "balance": balance,
        "reference": reference,
        "dedupeKey": sha256(dedupe_source.encode("utf-8")).hexdigest() if status != "parse_error" else "",
        "parseConfidence": round(max(0.0, min(confidence, 0.98)), 4),
        "status": status,
        "errorSummary": error_summary,
        "rawPayload": {"tableRow": table_row, "layoutProfile": profile["id"], "amountTexts": [match.group(0) for match in amount_matches]},
    }


def recover_bank_statement_pdf_rows(extraction: dict[str, Any], *, currency: str = "USD") -> dict[str, Any]:
    """Recover normalized review rows from text-layer PDF extraction output."""
    default_currency = (currency or "USD").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", default_currency):
        default_currency = "USD"
    if extraction.get("requiresOcr") and not bool((extraction.get("metadata") or {}).get("usesOcr")):
        return {
            "rows": [],
            "count": 0,
            "parsedCount": 0,
            "reviewCount": 0,
            "ignoredCount": 0,
            "parseErrorCount": 0,
            "validation": {"balanceContinuity": {"checked": 0, "failed": 0, "warnings": []}},
            "layoutProfiles": [profile["id"] for profile in BANK_STATEMENT_LAYOUT_PROFILES],
        }

    rows = [_recover_normalized_pdf_row(row, default_currency) for row in extraction.get("tableRows") or []]
    warnings: list[dict[str, Any]] = []
    checked = 0
    failed = 0
    previous_balance: float | None = None
    for row in rows:
        balance = row.get("balance")
        if not isinstance(balance, (int, float)):
            continue
        amount = float(row.get("amount") or 0.0)
        if previous_balance is not None and row.get("status") in {"parsed", "needs_review"}:
            checked += 1
            expected = round(previous_balance + amount, 4)
            if not isclose(expected, float(balance), abs_tol=0.02):
                failed += 1
                row["status"] = "needs_review"
                row["errorSummary"] = "balance continuity check failed"
                row["parseConfidence"] = round(min(float(row.get("parseConfidence") or 0.0), 0.58), 4)
                warnings.append({
                    "sourceRowNumber": row.get("sourceRowNumber"),
                    "expectedBalance": expected,
                    "actualBalance": round(float(balance), 4),
                    "amount": amount,
                })
        previous_balance = round(float(balance), 4)

    return {
        "rows": rows,
        "count": len(rows),
        "parsedCount": sum(1 for row in rows if row.get("status") == "parsed"),
        "reviewCount": sum(1 for row in rows if row.get("status") == "needs_review"),
        "ignoredCount": sum(1 for row in rows if row.get("status") == "ignored"),
        "parseErrorCount": sum(1 for row in rows if row.get("status") == "parse_error"),
        "validation": {"balanceContinuity": {"checked": checked, "failed": failed, "warnings": warnings}},
        "layoutProfiles": [profile["id"] for profile in BANK_STATEMENT_LAYOUT_PROFILES],
    }


def _attach_row_recovery(result: dict[str, Any], currency: str) -> dict[str, Any]:
    row_recovery = recover_bank_statement_pdf_rows(result, currency=currency)
    result["rows"] = row_recovery["rows"]
    result["rowRecovery"] = {key: value for key, value in row_recovery.items() if key != "rows"}
    return result


def _manual_review_ocr_result(
    result: dict[str, Any],
    *,
    attempted: bool,
    status: str,
    failure_reason: str,
    confidence: float = 0.0,
    max_pages: int = DEFAULT_BANK_STATEMENT_OCR_MAX_PAGES,
    timeout_seconds: float = DEFAULT_BANK_STATEMENT_OCR_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    result["status"] = "requires_manual_review"
    result["requiresManualReview"] = True
    result["ocr"] = {
        "attempted": attempted,
        "required": bool(result.get("requiresOcr")),
        "status": status,
        "engine": "paddleocr-pdf" if attempted else "not-run",
        "confidence": round(max(0.0, min(float(confidence or 0.0), 1.0)), 4),
        "pageCount": int(result.get("pageCount") or 0),
        "processedPageCount": int(result.get("processedPageCount") or 0),
        "maxPages": max_pages,
        "timeoutSeconds": timeout_seconds,
        "retryAttempts": 0,
        "failureReason": failure_reason,
    }
    result["metadata"]["ocrFailureReason"] = failure_reason
    return result


def _build_ocr_text_extraction_result(
    text: str,
    *,
    filename: str,
    currency: str,
    page_count: int,
    encrypted: bool,
    ocr_confidence: float,
    max_pages: int,
    max_text_chars: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    bounded_text = text[:max(1_000, int(max_text_chars or 120_000))]
    pages: list[dict[str, Any]] = []
    all_lines: list[dict[str, Any]] = []
    table_rows: list[dict[str, Any]] = []
    extracted_page_count = 0

    # PaddleOCR currently returns flat text for this path. Preserve line
    # evidence and mark the page as OCR-derived rather than inventing geometry.
    page_number = 1
    lines: list[dict[str, Any]] = []
    for raw_line in bounded_text.splitlines()[:500]:
        line = _clean_line(raw_line)[:512]
        if not line:
            continue
        line_number = len(lines) + 1
        line_payload = {"pageNumber": page_number, "lineNumber": line_number, "text": line, "source": "ocr"}
        lines.append(line_payload)
        all_lines.append(line_payload)
        row = _extract_possible_table_row(page_number, line_number, len(table_rows) + 1, line)
        if row:
            row["source"] = "ocr"
            table_rows.append(row)
    if lines:
        extracted_page_count = 1
    pages.append({
        "pageNumber": page_number,
        "width": None,
        "height": None,
        "text": "\n".join(line["text"] for line in lines),
        "lines": lines,
        "lineCount": len(lines),
        "charCount": sum(len(line["text"]) for line in lines),
        "source": "ocr",
    })

    text_char_count = sum(page["charCount"] for page in pages)
    compact_text_chars = sum(len(re.sub(r"\s+", "", page["text"])) for page in pages)
    bounded_confidence = max(0.0, min(float(ocr_confidence or 0.0), 1.0))
    table_confidence = 0.0 if not all_lines else min(0.95, len(table_rows) / max(len(all_lines), 1) + 0.30)
    status = "ocr_extracted" if table_rows else "requires_manual_review"
    result = {
        "fileName": filename,
        "status": status,
        "requiresOcr": True,
        "requiresManualReview": status == "requires_manual_review",
        "encrypted": encrypted,
        "pageCount": page_count,
        "processedPageCount": min(max(page_count, 1), max_pages),
        "extractedPageCount": extracted_page_count,
        "pages": pages,
        "lines": all_lines,
        "tableRows": table_rows,
        "confidence": {
            "text": round(bounded_confidence, 4),
            "tableStructure": round(table_confidence, 4),
            "overall": round(min(bounded_confidence, max(table_confidence, 0.35)), 4),
        },
        "metadata": {
            "engine": "paddleocr-pdf",
            "usesOcr": True,
            "textCharCount": text_char_count,
            "compactTextCharCount": compact_text_chars,
            "maxPages": max_pages,
            "maxTextChars": int(max_text_chars or 120_000),
            "jobType": "pdf_ocr",
        },
        "ocr": {
            "attempted": True,
            "required": True,
            "status": "succeeded" if table_rows else "no_transaction_rows",
            "engine": "paddleocr-pdf",
            "confidence": round(bounded_confidence, 4),
            "pageCount": page_count,
            "processedPageCount": min(max(page_count, 1), max_pages),
            "maxPages": max_pages,
            "timeoutSeconds": timeout_seconds,
            "retryAttempts": 0,
            "failureReason": "" if table_rows else "OCR text did not contain recognizable statement transaction rows",
        },
    }
    return _attach_row_recovery(result, currency)


def extract_bank_statement_text(
    text: str,
    *,
    filename: str,
    currency: str,
    page_count: int,
    ocr_engine: str,
    ocr_confidence: float,
    max_text_chars: int = 120_000,
) -> dict[str, Any]:
    """Build bounded bank-import proposals from text already extracted by the OCR worker.

    This path accepts no source bytes and performs no PDF parsing or rendering.
    """
    bounded_text = str(text or "")[:max(1_000, min(int(max_text_chars or 120_000), 120_000))]
    bounded_pages = max(1, min(int(page_count or 1), 1))
    if ocr_engine != "pypdf":
        result = _build_ocr_text_extraction_result(
            bounded_text,
            filename=filename,
            currency=currency,
            page_count=bounded_pages,
            encrypted=False,
            ocr_confidence=ocr_confidence,
            max_pages=1,
            max_text_chars=max_text_chars,
            timeout_seconds=30.0,
        )
        return _bounded_statement_proposal(result)

    lines: list[dict[str, Any]] = []
    table_rows: list[dict[str, Any]] = []
    for raw_line in bounded_text.splitlines()[:500]:
        line = _clean_line(raw_line)[:512]
        if not line:
            continue
        line_number = len(lines) + 1
        line_payload = {"pageNumber": 1, "lineNumber": line_number, "text": line}
        lines.append(line_payload)
        row = _extract_possible_table_row(1, line_number, len(table_rows) + 1, line)
        if row:
            table_rows.append(row)

    compact_chars = sum(len(re.sub(r"\s+", "", row["text"])) for row in lines)
    requires_ocr = compact_chars < TEXT_LAYER_THRESHOLD_COMPACT_CHARS
    table_confidence = 0.0 if not lines else min(0.95, len(table_rows) / max(len(lines), 1) + 0.35)
    text_confidence = 0.0 if requires_ocr else min(1.0, 0.72 + min(compact_chars, 2_000) / 8_000)
    result = {
        "fileName": filename,
        "status": "requires_manual_review" if requires_ocr else "extracted",
        "requiresOcr": requires_ocr,
        "requiresManualReview": requires_ocr,
        "encrypted": False,
        "pageCount": bounded_pages,
        "processedPageCount": bounded_pages,
        "extractedPageCount": 1 if lines else 0,
        "pages": [{
            "pageNumber": 1,
            "width": None,
            "height": None,
            "text": "\n".join(row["text"] for row in lines),
            "lines": lines,
            "lineCount": len(lines),
            "charCount": sum(len(row["text"]) for row in lines),
        }],
        "lines": lines,
        "tableRows": [] if requires_ocr else table_rows,
        "confidence": {
            "text": round(text_confidence, 4),
            "tableStructure": round(table_confidence, 4) if not requires_ocr else 0.0,
            "overall": round(text_confidence if not requires_ocr else 0.0, 4),
        },
        "metadata": {
            "engine": "pypdf",
            "usesOcr": False,
            "textCharCount": sum(len(row["text"]) for row in lines),
            "compactTextCharCount": compact_chars,
            "maxPages": 1,
            "maxTextChars": min(int(max_text_chars or 120_000), 120_000),
            "jobType": "pdf_text_layer",
        },
        "ocr": {
            "attempted": False,
            "required": requires_ocr,
            "status": "not_required" if not requires_ocr else "not_requested",
            "engine": "not-run",
            "confidence": 0.0,
            "pageCount": bounded_pages,
            "processedPageCount": bounded_pages,
            "maxPages": 1,
            "timeoutSeconds": 30.0,
            "retryAttempts": 0,
            "failureReason": "PDF has no usable selectable text; OCR was not requested" if requires_ocr else "",
        },
    }
    return _bounded_statement_proposal(_attach_row_recovery(result, currency))


def _bounded_statement_proposal(result: dict[str, Any]) -> dict[str, Any]:
    rows = list(result.get("rows") or [])[:200]
    row_recovery = dict(result.get("rowRecovery") or {})
    if int(result.get("rowRecovery", {}).get("count") or 0) > len(rows):
        result["requiresManualReview"] = True
        result["status"] = "requires_manual_review"
        result.setdefault("metadata", {})["rowLimitApplied"] = True
    row_recovery.update({
        "count": len(rows),
        "parsedCount": sum(row.get("status") == "parsed" for row in rows),
        "reviewCount": sum(row.get("status") == "needs_review" for row in rows),
        "ignoredCount": sum(row.get("status") == "ignored" for row in rows),
        "parseErrorCount": sum(row.get("status") == "parse_error" for row in rows),
    })
    return {
        "fileName": str(result.get("fileName") or "statement.pdf")[:255],
        "status": str(result.get("status") or "requires_manual_review")[:40],
        "requiresOcr": bool(result.get("requiresOcr")),
        "requiresManualReview": bool(result.get("requiresManualReview")),
        "pageCount": max(0, min(int(result.get("pageCount") or 0), 1)),
        "processedPageCount": max(0, min(int(result.get("processedPageCount") or 0), 1)),
        "extractedPageCount": max(0, min(int(result.get("extractedPageCount") or 0), 1)),
        "rows": rows,
        "rowRecovery": row_recovery,
        "confidence": dict(result.get("confidence") or {}),
        "metadata": dict(result.get("metadata") or {}),
        "ocr": dict(result.get("ocr") or {}),
    }


def _reader_for_pdf(payload: bytes, password: str | None):
    from pypdf import PdfReader

    if not payload.startswith(b"%PDF-"):
        raise PDFExtractionError("PDF_INVALID", "file must be a PDF bank statement")
    try:
        reader = PdfReader(io.BytesIO(payload), strict=False)
    except Exception as error:
        raise PDFExtractionError("PDF_INVALID", "PDF could not be parsed") from error

    if not reader.is_encrypted:
        return reader

    supplied_password = (password or "").strip()
    if not supplied_password:
        raise PDFExtractionError("PDF_PASSWORD_REQUIRED", "password-protected PDFs require a password for this extraction request")
    try:
        decrypted = reader.decrypt(supplied_password)
    except Exception as error:
        raise PDFExtractionError("PDF_PASSWORD_INVALID", "PDF password was rejected") from error
    if not decrypted:
        raise PDFExtractionError("PDF_PASSWORD_INVALID", "PDF password was rejected")
    return reader


def extract_bank_statement_pdf_text(
    payload: bytes,
    *,
    filename: str = "statement.pdf",
    password: str | None = None,
    currency: str = "USD",
    max_pages: int = 60,
    max_text_chars: int = 120_000,
) -> dict[str, Any]:
    """Extract selectable text and row-like line candidates from a PDF.

    The output is review evidence for CMD. It is not a normalized ledger
    transaction payload.
    """
    reader = _reader_for_pdf(payload, password)
    page_count = len(reader.pages)
    bounded_max_pages = max(1, min(int(max_pages or 1), 100))
    if page_count <= 0:
        raise PDFExtractionError("PDF_NO_PAGES", "PDF does not contain any readable pages")
    if page_count > bounded_max_pages:
        page_word = "page" if bounded_max_pages == 1 else "pages"
        raise PDFExtractionError("PDF_TOO_MANY_PAGES", f"PDF statements are limited to {bounded_max_pages} {page_word} for this import")
    bounded_pages = min(bounded_max_pages, page_count)
    remaining = max(1_000, int(max_text_chars or 120_000))

    pages: list[dict[str, Any]] = []
    all_lines: list[dict[str, Any]] = []
    table_rows: list[dict[str, Any]] = []
    extracted_page_count = 0

    for page_index, page in enumerate(reader.pages[:bounded_pages], start=1):
        if remaining <= 0:
            break
        try:
            raw_text = page.extract_text() or ""
        except Exception:
            raw_text = ""
        raw_text = raw_text[:remaining]
        remaining -= len(raw_text)

        lines: list[dict[str, Any]] = []
        for raw_line in raw_text.splitlines():
            line = _clean_line(raw_line)
            if not line:
                continue
            line_number = len(lines) + 1
            line_payload = {"pageNumber": page_index, "lineNumber": line_number, "text": line}
            lines.append(line_payload)
            all_lines.append(line_payload)
            row = _extract_possible_table_row(page_index, line_number, len(table_rows) + 1, line)
            if row:
                table_rows.append(row)
        if lines:
            extracted_page_count += 1
        pages.append({
            "pageNumber": page_index,
            "width": _safe_page_dimension(page, "width"),
            "height": _safe_page_dimension(page, "height"),
            "text": "\n".join(line["text"] for line in lines),
            "lines": lines,
            "lineCount": len(lines),
            "charCount": sum(len(line["text"]) for line in lines),
        })

    text_char_count = sum(page["charCount"] for page in pages)
    compact_text_chars = sum(len(re.sub(r"\s+", "", page["text"])) for page in pages)
    requires_ocr = compact_text_chars < TEXT_LAYER_THRESHOLD_COMPACT_CHARS
    table_confidence = 0.0 if not all_lines else min(0.95, len(table_rows) / max(len(all_lines), 1) + 0.35)
    text_confidence = 0.0 if requires_ocr else min(1.0, 0.72 + min(compact_text_chars, 2_000) / 8_000)

    result = {
        "fileName": filename,
        "status": "requires_ocr" if requires_ocr else "extracted",
        "requiresOcr": requires_ocr,
        "requiresManualReview": requires_ocr,
        "encrypted": bool(reader.is_encrypted),
        "pageCount": page_count,
        "processedPageCount": len(pages),
        "extractedPageCount": extracted_page_count,
        "pages": pages,
        "lines": all_lines,
        "tableRows": [] if requires_ocr else table_rows,
        "confidence": {
            "text": round(text_confidence, 4),
            "tableStructure": round(table_confidence, 4) if not requires_ocr else 0.0,
            "overall": round(text_confidence if not requires_ocr else 0.0, 4),
        },
        "metadata": {
            "engine": "pypdf",
            "usesOcr": False,
            "textCharCount": text_char_count,
            "compactTextCharCount": compact_text_chars,
            "maxPages": bounded_pages,
            "maxTextChars": int(max_text_chars or 120_000),
            "jobType": "pdf_text_layer",
        },
        "ocr": {
            "attempted": False,
            "required": requires_ocr,
            "status": "not_required" if not requires_ocr else "not_requested",
            "engine": "not-run",
            "confidence": 0.0,
            "pageCount": page_count,
            "processedPageCount": len(pages),
            "maxPages": DEFAULT_BANK_STATEMENT_OCR_MAX_PAGES,
            "timeoutSeconds": DEFAULT_BANK_STATEMENT_OCR_TIMEOUT_SECONDS,
            "retryAttempts": 0,
            "failureReason": "" if not requires_ocr else "PDF has no usable selectable text; OCR was not requested",
        },
    }
    return _attach_row_recovery(result, currency)


async def extract_bank_statement_pdf_with_optional_ocr(
    payload: bytes,
    *,
    filename: str = "statement.pdf",
    password: str | None = None,
    currency: str = "USD",
    enable_ocr: bool = False,
    max_pages: int = 60,
    max_text_chars: int = 120_000,
    max_ocr_pages: int = DEFAULT_BANK_STATEMENT_OCR_MAX_PAGES,
    ocr_timeout_seconds: float = DEFAULT_BANK_STATEMENT_OCR_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Extract a bank-statement PDF, optionally running bounded OCR if scanned.

    OCR is deliberately a second-stage job. Text-layer PDFs never invoke OCR,
    and OCR failures return a manual-review result instead of raising after the
    import run has already been created by CMD.
    """
    text_result = extract_bank_statement_pdf_text(
        payload,
        filename=filename,
        password=password,
        currency=currency,
        max_pages=max_pages,
        max_text_chars=max_text_chars,
    )
    bounded_ocr_pages = max(1, min(int(max_ocr_pages or DEFAULT_BANK_STATEMENT_OCR_MAX_PAGES), 100))
    bounded_timeout = max(1.0, min(float(ocr_timeout_seconds or DEFAULT_BANK_STATEMENT_OCR_TIMEOUT_SECONDS), 120.0))
    if not text_result.get("requiresOcr"):
        text_result["ocr"]["maxPages"] = bounded_ocr_pages
        text_result["ocr"]["timeoutSeconds"] = bounded_timeout
        return text_result
    if not enable_ocr:
        return _manual_review_ocr_result(
            text_result,
            attempted=False,
            status="not_requested",
            failure_reason="PDF has no usable selectable text; OCR was not requested",
            max_pages=bounded_ocr_pages,
            timeout_seconds=bounded_timeout,
        )

    temp = tempfile.NamedTemporaryFile(prefix="ledgerical-bank-statement-ocr-", suffix=".pdf", delete=False)
    temp_path = temp.name
    try:
        temp.write(payload)
        temp.flush()
        temp.close()
        try:
            ocr_text, ocr_confidence = await asyncio.wait_for(
                ocr_pdf(temp_path, max_pages=bounded_ocr_pages),
                timeout=bounded_timeout,
            )
        except asyncio.TimeoutError:
            return _manual_review_ocr_result(
                text_result,
                attempted=True,
                status="timeout",
                failure_reason=f"OCR exceeded the {int(bounded_timeout)} second timeout",
                max_pages=bounded_ocr_pages,
                timeout_seconds=bounded_timeout,
            )
        except Exception as error:
            return _manual_review_ocr_result(
                text_result,
                attempted=True,
                status="failed",
                failure_reason=f"OCR failed: {str(error)[:240]}",
                max_pages=bounded_ocr_pages,
                timeout_seconds=bounded_timeout,
            )
    finally:
        try:
            temp.close()
        except Exception:
            pass
        try:
            os.unlink(temp_path)
        except OSError:
            pass

    compact_ocr_text = re.sub(r"\s+", "", ocr_text or "")
    if len(compact_ocr_text) < TEXT_LAYER_THRESHOLD_COMPACT_CHARS:
        return _manual_review_ocr_result(
            text_result,
            attempted=True,
            status="no_text",
            failure_reason="OCR did not recover enough readable text from this scanned PDF",
            confidence=float(ocr_confidence or 0.0),
            max_pages=bounded_ocr_pages,
            timeout_seconds=bounded_timeout,
        )
    return _build_ocr_text_extraction_result(
        ocr_text,
        filename=filename,
        currency=currency,
        page_count=int(text_result.get("pageCount") or 0),
        encrypted=bool(text_result.get("encrypted")),
        ocr_confidence=float(ocr_confidence or 0.0),
        max_pages=bounded_ocr_pages,
        max_text_chars=max_text_chars,
        timeout_seconds=bounded_timeout,
    )
