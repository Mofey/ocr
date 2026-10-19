"""One-shot adapter. Only the trusted Vercel Function holds the signing key.

Input/output bind mounts are RAM-only; nothing is emitted to command logs.
No HTTP server, app imports, credentials, storage clients or deployment SDK.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .app import (
    MAX_CHILD_IPC_BYTES, MAX_OCR_INPUT_BYTES, OCRWorkerResult,
    TransientReceiptResult, _run_isolated_child,
)
from .grant import OCRJobGrant, TransientReceiptGrant
from .isolation import enforce_worker_runtime, prepare_parser_sandbox

MAX_ENVELOPE_BYTES = 3 * 1024 * 1024


def _execute() -> dict:
    with Path('/input/job.json').open('rb') as stream:
        raw = stream.read(MAX_ENVELOPE_BYTES + 1)
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise ValueError('input limit')
    envelope = json.loads(raw)
    if isinstance(envelope, dict) and envelope.get('grant', {}).get('schemaVersion') == 3:
        if set(envelope) != {'grant', 'contentFile'} or envelope['contentFile'] != 'source.bin':
            raise ValueError('file input schema')
        claims = envelope['grant']
        if claims.get('purpose') not in {'validate', 'tabular', 'signature-image', 'public-image'}:
            raise ValueError('file purpose')
        with Path('/input/source.bin').open('rb') as source:
            body = source.read(10 * 1024 * 1024 + 1)
        if len(body) > 10 * 1024 * 1024:
            raise ValueError('file input limit')
        prepare_parser_sandbox()
        from .file_processor import process_file
        try:
            return {'status': 200, 'result': process_file(claims, body)}
        except ImportError:
            return {'status': 503, 'error': 'OCR_WORKER_UNAVAILABLE'}
        except Exception:
            return {'status': 422, 'error': 'INVALID_DOCUMENT_FORMAT'}
    if not isinstance(envelope, dict) or set(envelope) != {'grant', 'contentBase64'}:
        raise ValueError('input schema')
    claims = envelope['grant']
    if not isinstance(claims, dict):
        raise ValueError('grant schema')
    grant_type = TransientReceiptGrant if claims.get('schemaVersion') == 2 else OCRJobGrant
    grant = grant_type.model_validate(claims)
    grant.validate_lifetime()
    body = base64.b64decode(envelope['contentBase64'], validate=True)
    if len(body) != grant.size_bytes or len(body) > MAX_OCR_INPUT_BYTES:
        return {'status': 409, 'error': 'OCR_INPUT_SIZE_MISMATCH'}
    if hashlib.sha256(body).hexdigest() != grant.sha256:
        return {'status': 409, 'error': 'SOURCE_HASH_MISMATCH'}
    scratch = tempfile.mkdtemp(prefix='ocr-job-', dir='/tmp')
    try:
        try:
            outcome, elapsed = _run_isolated_child(body, grant, scratch, 25)
        except TimeoutError:
            return {'status': 504, 'error': 'OCR_WORKER_TIMEOUT'}
        if outcome.get('outcome') == 'format_error':
            # Never forward the parser's exception text or arbitrary error code.
            codes = {'INVALID_DOCUMENT_FORMAT': 422, 'UNSUPPORTED_DOCUMENT_FORMAT': 415,
                     'SOURCE_HASH_MISMATCH': 409}
            code = outcome.get('code')
            return {'status': codes[code], 'error': code} if code in codes else {'status': 422, 'error': 'INVALID_DOCUMENT_FORMAT'}
        if outcome.get('outcome') != 'ok':
            return {'status': 503, 'error': 'OCR_WORKER_UNAVAILABLE'}
        payload = outcome['result']
        payload['metrics']['wallClockMs'] = elapsed
        result_type = TransientReceiptResult if isinstance(grant, TransientReceiptGrant) else OCRWorkerResult
        result = result_type.model_validate(payload).model_dump(by_alias=True, mode='json')
        ids = ['requestId'] if isinstance(grant, TransientReceiptGrant) else ['jobId', 'documentId', 'versionId']
        expected = grant.model_dump(by_alias=True, mode='json')
        if any(result[key] != expected[key] for key in ['workspaceId', *ids]) or (
            result['sourceSha256'] != grant.sha256 or result['sourceSizeBytes'] != grant.size_bytes
            or result['detectedContentType'] != grant.content_type or result['contentValidated'] is not True
        ):
            raise ValueError('output binding')
        return {'status': 200, 'result': result}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> int:
    # All untrusted/native output stays out of SDK command logs.
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    if null > 2:
        os.close(null)
    enforce_worker_runtime()
    # PaddleX keeps lock/temp/function-cache paths below its model cache.
    # Weights stay read-only; these three image symlinks target this job's tmpfs.
    for directory in ('/tmp/paddle-locks', '/tmp/paddle-temp', '/tmp/paddle-functions'):
        Path(directory).mkdir(mode=0o700, exist_ok=True)
    if sys.argv[1:] == ['--probe']:
        prepare_parser_sandbox()
        return 0
    if sys.argv[1:]:
        return 1
    try:
        output = _execute()
        encoded = json.dumps(output, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
        if len(encoded) > 3 * 1024 * 1024:
            raise ValueError('output limit')
    except BaseException:
        encoded = b'{"status":503,"error":"OCR_WORKER_UNAVAILABLE"}'
    # umask/readability allows only this otherwise empty VM's SDK to retrieve it.
    os.umask(0o022)
    with Path('/output/result.json').open('xb') as stream:
        stream.write(encoded)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
