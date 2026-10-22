"""Exercise real decoders inside the worker, never in CMD/Main."""
from __future__ import annotations

import base64
import hashlib
import io
import zipfile

import pytest
from PIL import Image, PngImagePlugin

from ocr_worker.file_processor import process_file


def image_bytes(*, blank=False, transparent=False, metadata=False):
    image = Image.new('RGBA', (40, 20), (255, 255, 255, 0 if transparent else 255))
    if not blank:
        for x in range(2, 36):
            image.putpixel((x, 10), (20, 20, 20, 255))
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    if metadata:
        info.add_text('Comment', 'private source metadata')
    image.save(output, format='PNG', pnginfo=info, icc_profile=b'private color profile' if metadata else None)
    return output.getvalue()


def claims(body, *, filename='signature.png', content_type='image/png', purpose='signature-image'):
    return {'filename': filename, 'contentType': content_type, 'purpose': purpose,
            'sizeBytes': len(body), 'sha256': hashlib.sha256(body).hexdigest(),
            'workspaceId': 'workspace', 'requestId': 'request'}


@pytest.mark.parametrize('transparent', [False, True])
def test_blank_signature_is_rejected(transparent):
    body = image_bytes(blank=True, transparent=transparent)
    with pytest.raises(ValueError, match='empty signature'):
        process_file(claims(body), body)


def test_signature_with_spoofed_mime_is_rejected():
    body = image_bytes()
    with pytest.raises(ValueError, match='image limits'):
        process_file(claims(body, filename='signature.jpg', content_type='image/jpeg'), body)


@pytest.mark.parametrize('purpose', ['signature-image', 'public-image'])
def test_normalization_strips_source_metadata_and_returns_png(purpose):
    body = image_bytes(metadata=True)
    result = process_file(claims(body, purpose=purpose), body)
    output = base64.b64decode(result['imageBase64'], validate=True)
    with Image.open(io.BytesIO(output)) as image:
        assert image.format == 'PNG'
        assert image.size == (40, 20)
        assert 'Comment' not in image.info
        assert 'icc_profile' not in image.info
    assert result['records'] == []
    assert result['contentValidated'] is True


@pytest.mark.parametrize('mutate', [
    lambda value: value.update(sha256='0' * 64),
    lambda value: value.update(sizeBytes=value['sizeBytes'] + 1),
    lambda value: value.update(contentType='application/pdf'),
])
def test_worker_rejects_unbound_source_bytes(mutate):
    body = image_bytes()
    grant = claims(body)
    mutate(grant)
    with pytest.raises(ValueError, match='source mismatch'):
        process_file(grant, body)


def test_csv_decoding_returns_structured_records_for_cmd():
    body = b'Date,Description,Amount\n2026-08-01,Payment,100.00\n'
    result = process_file(claims(body, filename='statement.csv', content_type='text/csv', purpose='tabular'), body)
    assert result['records'] == [['Date', 'Description', 'Amount'], ['2026-08-01', 'Payment', '100.00']]
    assert result['imageBase64'] == ''


def test_invalid_csv_encoding_is_rejected():
    body = b'column\n\xff'
    with pytest.raises(UnicodeDecodeError):
        process_file(claims(body, filename='statement.csv', content_type='text/csv', purpose='tabular'), body)


@pytest.mark.parametrize('macro', [False, True])
def test_office_structure_and_macro_policy_run_in_worker(macro):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types><Override ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        if macro:
            archive.writestr('word/vbaProject.bin', b'macro')
    body = output.getvalue()
    grant = claims(body, filename='document.docx', content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document', purpose='validate')
    if macro:
        with pytest.raises(ValueError, match='macro package'):
            process_file(grant, body)
    else:
        assert process_file(grant, body)['contentValidated'] is True


def test_pdf_structure_runs_in_worker():
    from pypdf import PdfWriter
    from pypdf.errors import PdfReadError
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(output)
    body = output.getvalue()
    assert process_file(claims(body, filename='document.pdf', content_type='application/pdf', purpose='validate'), body)['contentValidated'] is True
    invalid = b'%PDF-1.7\nbody\n%%EOF\n'
    with pytest.raises((ValueError, PdfReadError)):
        process_file(claims(invalid, filename='document.pdf', content_type='application/pdf', purpose='validate'), invalid)
