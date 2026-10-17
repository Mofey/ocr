from __future__ import annotations

import asyncio
import io
import json

import pytest
from pypdf import PdfReader, PdfWriter

from ocr_worker import bank_statement_pdf
from ocr_worker.bank_statement_pdf import (
    PDFExtractionError,
    extract_bank_statement_pdf_text,
    extract_bank_statement_pdf_with_optional_ocr,
)


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_bytes(lines: list[str]) -> bytes:
    stream_lines = ["BT", "/F1 12 Tf", "72 720 Td"]
    for index, line in enumerate(lines):
        if index:
            stream_lines.append("0 -18 Td")
        stream_lines.append(f"({_escape_pdf_text(line)}) Tj")
    stream_lines.append("ET")
    stream = "\n".join(stream_lines).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    parts = [b"%PDF-1.4\n"]
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(sum(len(part) for part in parts))
        parts.append(f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n")
    xref_offset = sum(len(part) for part in parts)
    xref = [
        b"xref\n",
        f"0 {len(objects) + 1}\n".encode("ascii"),
        b"0000000000 65535 f \n",
    ]
    xref.extend(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:])
    trailer = (
        b"trailer\n"
        + f"<< /Root 1 0 R /Size {len(objects) + 1} >>\n".encode("ascii")
        + b"startxref\n"
        + str(xref_offset).encode("ascii")
        + b"\n%%EOF\n"
    )
    return b"".join(parts + xref + [trailer])


def _statement_pdf() -> bytes:
    return _pdf_bytes([
        "Ledgerical Bank Statement",
        "Date Description Money Out Money In Balance",
        "2026-08-01 Opening balance 0.00 0.00 1,000.00",
        "2026-08-02 ACME CLIENT INV-42 0.00 250.00 1,250.00",
        "2026-08-03 OFFICE SUPPLIES 75.50 0.00 1,174.50",
    ])


def _encrypted_pdf(payload: bytes, password: str) -> bytes:
    reader = PdfReader(io.BytesIO(payload), strict=False)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(password)
    target = io.BytesIO()
    writer.write(target)
    return target.getvalue()


def _multi_page_pdf(payload: bytes, count: int) -> bytes:
    reader = PdfReader(io.BytesIO(payload), strict=False)
    writer = PdfWriter()
    for _ in range(count):
        writer.add_page(reader.pages[0])
    target = io.BytesIO()
    writer.write(target)
    return target.getvalue()


def test_text_layer_pdf_extracts_lines_and_candidate_rows() -> None:
    result = extract_bank_statement_pdf_text(_statement_pdf(), filename="statement.pdf", currency="GBP")

    assert result["status"] == "extracted"
    assert result["requiresOcr"] is False
    assert result["metadata"]["engine"] == "pypdf"
    assert result["metadata"]["usesOcr"] is False
    assert result["pageCount"] == 1
    assert result["pages"][0]["lineCount"] == 5
    assert any(line["text"] == "2026-08-02 ACME CLIENT INV-42 0.00 250.00 1,250.00" for line in result["lines"])
    assert len(result["tableRows"]) == 3
    assert result["tableRows"][1]["cells"][:2] == ["2026-08-02", "ACME CLIENT INV-42"]
    assert result["rowRecovery"]["parsedCount"] == 2
    assert result["rowRecovery"]["ignoredCount"] == 1
    assert result["rowRecovery"]["validation"]["balanceContinuity"]["checked"] == 2
    assert result["rowRecovery"]["validation"]["balanceContinuity"]["failed"] == 0
    assert result["rows"][1]["postedDate"] == "2026-08-02"
    assert result["rows"][1]["amount"] == 250.0
    assert result["rows"][1]["moneyIn"] == 250.0
    assert result["rows"][1]["currency"] == "GBP"
    assert result["rows"][1]["reference"] == "INV-42"
    assert result["confidence"]["text"] > 0.7


def test_single_signed_amount_pdf_layout_recovers_canonical_rows() -> None:
    pdf = _pdf_bytes([
        "Statement Transactions",
        "Date Details Amount Balance",
        "01/08/2026 Opening balance 0.00 1,000.00",
        "02/08/2026 POS PURCHASE -24.99 975.01",
        "03/08/2026 SALARY +500.00 1,475.01",
    ])

    result = extract_bank_statement_pdf_text(pdf, filename="signed-layout.pdf", currency="USD")

    assert result["rowRecovery"]["parsedCount"] == 2
    assert result["rowRecovery"]["ignoredCount"] == 1
    assert result["rowRecovery"]["validation"]["balanceContinuity"]["failed"] == 0
    assert result["rows"][1]["postedDate"] == "2026-08-02"
    assert result["rows"][1]["amount"] == -24.99
    assert result["rows"][1]["moneyOut"] == 24.99
    assert result["rows"][2]["amount"] == 500.0
    assert result["rows"][2]["moneyIn"] == 500.0


def test_reference_debit_credit_pdf_layout_preserves_source_evidence() -> None:
    pdf = _pdf_bytes([
        "Transactions",
        "Date Ref Narration Withdrawal Deposit Balance",
        "03 Aug 2026 REF-7721 Vendor Bill 80.00 0.00 920.00",
        "04 Aug 2026 TRN-102 Client Deposit 0.00 300.00 1,220.00",
    ])

    result = extract_bank_statement_pdf_text(pdf, filename="reference-layout.pdf", currency="NGN")

    assert result["rowRecovery"]["parsedCount"] == 2
    assert result["rows"][0]["postedDate"] == "2026-08-03"
    assert result["rows"][0]["amount"] == -80.0
    assert result["rows"][0]["reference"] == "REF-7721"
    assert result["rows"][0]["evidence"]["pageNumber"] == 1
    assert result["rows"][0]["evidence"]["sourceText"].startswith("03 Aug 2026 REF-7721")
    assert result["rows"][1]["amount"] == 300.0
    assert result["rows"][1]["balance"] == 1220.0


def test_balance_continuity_failure_marks_row_for_review_with_evidence() -> None:
    pdf = _pdf_bytes([
        "Statement Transactions",
        "Date Details Amount Balance",
        "01/08/2026 Opening balance 0.00 1,000.00",
        "02/08/2026 POS PURCHASE -24.99 900.00",
    ])

    result = extract_bank_statement_pdf_text(pdf, filename="bad-balance.pdf", currency="USD")

    assert result["rowRecovery"]["validation"]["balanceContinuity"]["failed"] == 1
    assert result["rows"][1]["status"] == "needs_review"
    assert result["rows"][1]["errorSummary"] == "balance continuity check failed"
    assert result["rows"][1]["evidence"]["lineNumber"] == 4


def test_blank_pdf_is_marked_requires_ocr_without_table_rows() -> None:
    result = extract_bank_statement_pdf_text(_pdf_bytes([]), filename="scan.pdf")

    assert result["status"] == "requires_ocr"
    assert result["requiresOcr"] is True
    assert result["tableRows"] == []
    assert result["metadata"]["usesOcr"] is False


def test_scanned_pdf_without_ocr_returns_manual_review_state() -> None:
    result = asyncio.run(
        extract_bank_statement_pdf_with_optional_ocr(_pdf_bytes([]), filename="scan.pdf", enable_ocr=False)
    )

    assert result["status"] == "requires_manual_review"
    assert result["requiresManualReview"] is True
    assert result["ocr"]["attempted"] is False
    assert result["ocr"]["status"] == "not_requested"
    assert result["rowRecovery"]["count"] == 0


def test_optional_ocr_recovers_rows_from_scanned_pdf(monkeypatch) -> None:
    captured: dict[str, int] = {}

    async def fake_ocr(_path: str, max_pages: int):
        captured["max_pages"] = max_pages
        return (
            "\n".join([
                "Scanned Bank Statement",
                "Date Details Amount Balance",
                "01/08/2026 Opening balance 0.00 1,000.00",
                "02/08/2026 POS PURCHASE -24.99 975.01",
                "03/08/2026 SALARY +500.00 1,475.01",
            ]),
            0.87,
        )

    monkeypatch.setattr(bank_statement_pdf, "ocr_pdf", fake_ocr)
    result = asyncio.run(
        extract_bank_statement_pdf_with_optional_ocr(
            _pdf_bytes([]),
            filename="scan.pdf",
            currency="USD",
            enable_ocr=True,
            max_ocr_pages=4,
            ocr_timeout_seconds=5,
        )
    )

    assert result["status"] == "ocr_extracted"
    assert result["metadata"]["usesOcr"] is True
    assert result["metadata"]["jobType"] == "pdf_ocr"
    assert result["ocr"]["attempted"] is True
    assert result["ocr"]["confidence"] == pytest.approx(0.87)
    assert captured["max_pages"] == 4
    assert result["rowRecovery"]["parsedCount"] == 2
    assert result["rowRecovery"]["ignoredCount"] == 1
    assert result["rows"][1]["evidence"]["sourceText"].startswith("02/08/2026 POS")


def test_optional_ocr_failure_preserves_manual_review_result(monkeypatch) -> None:
    async def fail_ocr(_path: str, max_pages: int):
        raise RuntimeError("render failed")

    monkeypatch.setattr(bank_statement_pdf, "ocr_pdf", fail_ocr)
    result = asyncio.run(
        extract_bank_statement_pdf_with_optional_ocr(
            _pdf_bytes([]),
            filename="scan.pdf",
            enable_ocr=True,
            ocr_timeout_seconds=5,
        )
    )

    assert result["status"] == "requires_manual_review"
    assert result["requiresManualReview"] is True
    assert result["ocr"]["attempted"] is True
    assert result["ocr"]["status"] == "failed"
    assert "render failed" in result["ocr"]["failureReason"]
    assert result["rowRecovery"]["count"] == 0


def test_encrypted_pdf_requires_one_request_password_and_does_not_echo_it() -> None:
    encrypted = _encrypted_pdf(_statement_pdf(), "secret-pass")

    with pytest.raises(PDFExtractionError) as missing:
        extract_bank_statement_pdf_text(encrypted, filename="locked.pdf")
    assert missing.value.code == "PDF_PASSWORD_REQUIRED"

    result = extract_bank_statement_pdf_text(encrypted, filename="locked.pdf", password="secret-pass")
    assert result["status"] == "extracted"
    assert "secret-pass" not in json.dumps(result)


def test_pdf_page_count_limit_fails_safely() -> None:
    oversized = _multi_page_pdf(_statement_pdf(), 2)

    with pytest.raises(PDFExtractionError) as error:
        extract_bank_statement_pdf_text(oversized, filename="long-statement.pdf", max_pages=1)

    assert error.value.code == "PDF_TOO_MANY_PAGES"
    assert "1 page" in error.value.message


def test_malformed_pdf_fails_safely() -> None:
    with pytest.raises(PDFExtractionError) as error:
        extract_bank_statement_pdf_text(b"not a pdf", filename="bad.pdf")

    assert error.value.code == "PDF_INVALID"
