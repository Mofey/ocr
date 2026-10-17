import io

import pytest
from PIL import Image
from pypdf import PdfWriter

from ocr_worker.processor import WorkerFormatError, _validate_image, _validate_pdf


def test_worker_rejects_multiple_pdf_pages_before_ocr():
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    writer.write(output)
    with pytest.raises(WorkerFormatError, match="exactly one page"):
        _validate_pdf(output.getvalue(), "receipt.pdf", "application/pdf")


def test_worker_rejects_large_decoded_image_and_mislabeled_bytes():
    output = io.BytesIO()
    Image.new("RGB", (2001, 2000)).save(output, format="PNG")
    with pytest.raises(WorkerFormatError) as rejected:
        _validate_image(output.getvalue(), "receipt.png", "image/png")
    assert rejected.value.code == "INVALID_DOCUMENT_FORMAT"
    small = io.BytesIO()
    Image.new("RGB", (10, 10)).save(small, format="PNG")
    with pytest.raises(WorkerFormatError, match="signature"):
        _validate_image(small.getvalue(), "receipt.jpg", "image/jpeg")
