"""Document decoders run only inside the disposable, network-denied container.

No app imports, storage credentials, subprocesses, macros or external fetches.
The trusted gateway authenticates the request and binds its bytes beforehand.
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import hashlib
import io
import json
import re
import stat
import warnings
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

MAX_EXPANDED = 64 * 1024 * 1024
MAX_TEXT = 2 * 1024 * 1024
MIME = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.csv': 'text/csv', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}


def _xml(data: bytes):
    if len(data) > 8 * 1024 * 1024 or re.search(br'<!\s*(?:DOCTYPE|ENTITY)', data, re.I):
        raise ValueError('xml limit')
    return ET.fromstring(data)


def _office(body: bytes, ext: str) -> dict[str, bytes]:
    members = {}
    expanded = 0
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        if not 0 < len(archive.infolist()) <= 2000:
            raise ValueError('entry limit')
        for info in archive.infolist():
            name = info.filename
            path = PurePosixPath(name)
            if not name or '\\' in name or '\x00' in name or path.is_absolute() or '..' in path.parts or ':' in name or info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16) or name in members:
                raise ValueError('unsafe archive')
            if 'vbaproject' in name.lower() or name.lower().endswith(('.xlsm', '.docm')):
                raise ValueError('macro package')
            if info.file_size > MAX_EXPANDED - expanded or (info.file_size > 1024 * 1024 and info.file_size > max(1, info.compress_size) * 100):
                raise ValueError('expansion limit')
            # Count ACTUAL decompressed bytes, including CRC and declared-size
            # verification by ZipExtFile. Never extract paths to the filesystem.
            with archive.open(info) as stream:
                data = stream.read(min(MAX_EXPANDED - expanded, 8 * 1024 * 1024) + 1)
            expanded += len(data)
            if len(data) != info.file_size or len(data) > 8 * 1024 * 1024 or expanded > MAX_EXPANDED:
                raise ValueError('expansion limit')
            members[name] = data
        types = members.get('[Content_Types].xml', b'')
        _xml(types)
        if b'macroenabled' in types.lower():
            raise ValueError('macro package')
        main = 'xl/workbook.xml' if ext == '.xlsx' else 'word/document.xml'
        expected = b'spreadsheetml.sheet.main+xml' if ext == '.xlsx' else b'wordprocessingml.document.main+xml'
        if main not in members or expected not in types.lower():
            raise ValueError('missing document')
        _xml(members[main])
    return members


def _cell(value: str) -> str:
    if '\x00' in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value) or len(value.encode('utf-8')) > 65536:
        raise ValueError('cell limit')
    return value


def _csv(body: bytes, collect: bool) -> list[list[str]]:
    text = body.decode('utf-8-sig')
    if '\x00' in text:
        raise ValueError('invalid text')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    delimiter = ','
    first, _, rest = text.partition('\n')
    if first.lower().startswith('sep=') and len(first.strip()) == 5:
        delimiter, text = first.strip()[4], rest
    else:
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=',;\t').delimiter
        except csv.Error:
            pass
    csv.field_size_limit(65536)
    records, count, total = [], 0, 0
    for row in csv.reader(io.StringIO(text, newline=''), delimiter=delimiter, skipinitialspace=True):
        count += 1
        if count > (10001 if collect else 100000) or len(row) > (200 if collect else 1000):
            raise ValueError('table limit')
        row = [_cell(value.strip().lstrip('\ufeff')) for value in row]
        if collect and any(row):
            total += sum(len(value.encode('utf-8')) for value in row)
            if total > MAX_TEXT:
                raise ValueError('output limit')
            records.append(row)
    return records


def _xlsx(members: dict[str, bytes]) -> list[list[str]]:
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    workbook = _xml(members['xl/workbook.xml'])
    sheet = workbook.find('s:sheets/s:sheet', ns)
    if sheet is None:
        raise ValueError('no worksheet')
    rid = sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
    rels = _xml(members.get('xl/_rels/workbook.xml.rels', b''))
    rel = next((r for r in rels if r.get('Id') == rid), None)
    if rel is None or rel.get('TargetMode', '').lower() == 'external':
        raise ValueError('external worksheet')
    target = rel.get('Target', '')
    path = target.lstrip('/') if target.startswith('/') else 'xl/' + target
    if '..' in PurePosixPath(path).parts or path not in members:
        raise ValueError('worksheet path')
    strings = []
    if 'xl/sharedStrings.xml' in members:
        for item in _xml(members['xl/sharedStrings.xml']):
            strings.append(_cell(''.join(t.text or '' for t in item.iter('{'+ns['s']+'}t'))))
            if len(strings) > 100000:
                raise ValueError('string limit')
    date_indexes = set()
    if 'xl/styles.xml' in members:
        styles = _xml(members['xl/styles.xml'])
        formats = {int(f.get('numFmtId', '0')): f.get('formatCode', '') for f in styles.findall('s:numFmts/s:numFmt', ns)}
        for index, style in enumerate(styles.findall('s:cellXfs/s:xf', ns)):
            fid = int(style.get('numFmtId', '0'))
            fmt = re.sub(r'"[^"]*"|\\.', '', formats.get(fid, '')).lower()
            if fid in set(range(14, 23)) | set(range(27, 37)) | {45, 46, 47} | set(range(50, 59)) or re.search(r'[ymdhis]', fmt):
                date_indexes.add(index)
    props = workbook.find('s:workbookPr', ns)
    date1904 = props is not None and props.get('date1904') in ('1', 'true')
    records, total = [], 0
    for element in _xml(members[path]).findall('s:sheetData/s:row', ns):
        if len(records) >= 10001:
            raise ValueError('row limit')
        row = []
        for cell in element.findall('s:c', ns):
            ref = re.fullmatch(r'([A-Za-z]{1,3})[1-9][0-9]*', cell.get('r', ''))
            if not ref:
                raise ValueError('cell reference')
            index = 0
            for char in ref[1].upper():
                index = index * 26 + ord(char) - 64
            if index > 200:
                raise ValueError('column limit')
            while len(row) < index:
                row.append('')
            kind = cell.get('t', '')
            value = cell.findtext('s:v', '', ns)
            if kind == 's':
                value = strings[int(value)]
            elif kind == 'inlineStr':
                value = ''.join(t.text or '' for t in cell.iter('{'+ns['s']+'}t'))
            elif kind == 'b':
                value = 'TRUE' if value == '1' else 'FALSE'
            elif kind in ('', 'n') and value and int(cell.get('s', '-1')) in date_indexes:
                serial = float(value)
                epoch = dt.datetime(1904, 1, 1) if date1904 else dt.datetime(1899, 12, 31 if serial < 60 else 30)
                date = epoch + dt.timedelta(days=serial)
                value = date.date().isoformat() if date.time() == dt.time() else date.isoformat() + 'Z'
            row[index - 1] = _cell(value)  # cached formula values only; no evaluation
        if any(row):
            total += sum(len(v.encode('utf-8')) for v in row)
            if total > MAX_TEXT:
                raise ValueError('output limit')
            records.append(row)
    return records


def process_file(claims: dict, body: bytes) -> dict:
    ext = PurePosixPath(claims['filename']).suffix.lower()
    purpose = claims['purpose']
    if MIME.get(ext) != claims['contentType'] or len(body) != claims['sizeBytes'] or hashlib.sha256(body).hexdigest() != claims['sha256']:
        raise ValueError('source mismatch')
    records, image = [], ''
    if ext in ('.png', '.jpg', '.jpeg', '.webp'):
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 4_000_000 if purpose == 'signature-image' else 16_000_000
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body)) as original:
                expected = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP'}[ext]
                if original.format != expected or original.width * original.height > Image.MAX_IMAGE_PIXELS or getattr(original, 'n_frames', 1) != 1:
                    raise ValueError('image limits')
                original.verify()
            if purpose in ('signature-image', 'public-image'):
                with Image.open(io.BytesIO(body)) as original:
                    canonical = original.convert('RGBA')
                    canonical.info.clear()
                    if purpose == 'signature-image':
                        if canonical.width > 4096 or canonical.height > 4096 or sum(1 for r, g, b, a in canonical.getdata() if a > 32 and min(r, g, b) < 235) <= 8:
                            raise ValueError('empty signature')
                    else:
                        canonical.thumbnail((1024, 1024))
                    output = io.BytesIO()
                    canonical.save(output, format='PNG')  # metadata stripped
                    while purpose == 'public-image' and output.tell() > 1024 * 1024 and max(canonical.size) > 128:
                        edge = max(128, int(max(canonical.size) * .75))
                        canonical.thumbnail((edge, edge))
                        output = io.BytesIO()
                        canonical.save(output, format='PNG')
                    if output.tell() > 1024 * 1024:
                        raise ValueError('image output limit')
                    image = base64.b64encode(output.getvalue()).decode('ascii')
    elif ext == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(body), strict=True)
        if reader.is_encrypted or not 0 < len(reader.pages) <= 100:
            raise ValueError('pdf limit')
    elif ext == '.csv':
        records = _csv(body, purpose == 'tabular')
    elif ext in ('.xlsx', '.docx'):
        members = _office(body, ext)
        if purpose == 'tabular':
            records = _xlsx(members)
    else:
        raise ValueError('unsupported format')
    result = {'schemaVersion': 3, 'workspaceId': claims['workspaceId'], 'requestId': claims['requestId'], 'purpose': purpose, 'sourceSha256': claims['sha256'], 'sourceSizeBytes': len(body), 'detectedContentType': claims['contentType'], 'contentValidated': True, 'records': records, 'imageBase64': image}
    if len(json.dumps(result, ensure_ascii=True).encode()) > 3 * 1024 * 1024:
        raise ValueError('output limit')
    return result
