from __future__ import annotations

import hashlib
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from ocr_worker import app as worker_app
from ocr_worker.grant import encode_test_grant


SECRET = "test-worker-grant-secret-for-failure-tests"
WORKSPACE = "22222222-2222-4222-8222-222222222222"
DOCUMENT = "33333333-3333-4333-8333-333333333333"
VERSION = "44444444-4444-4444-8444-444444444444"
JOB = "11111111-1111-4111-8111-111111111111"


def _request(body: bytes) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/process",
        "raw_path": b"/v1/process",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/octet-stream"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8010),
    }
    return Request(scope, receive)


def _grant(body: bytes) -> str:
    now = int(time.time())
    return encode_test_grant(
        {
            "schemaVersion": 1,
            "audience": "document-ocr-worker",
            "jobId": JOB,
            "workspaceId": WORKSPACE,
            "documentId": DOCUMENT,
            "versionId": VERSION,
            "objectKey": f"immutable/workspaces/{WORKSPACE}/documents/{DOCUMENT}/versions/{VERSION}",
            "filename": "invoice.pdf",
            "contentType": "application/pdf",
            "sizeBytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "issuedAt": now,
            "expiresAt": now + 60,
        },
        SECRET,
    )


@pytest.mark.anyio
async def test_worker_child_crash_returns_sanitized_unavailable_and_releases_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"%PDF-1.7\n"
    monkeypatch.setenv("OCR_WORKER_GRANT_SECRET", SECRET)
    monkeypatch.setattr(worker_app, "_active_job", False)
    monkeypatch.setattr(
        worker_app,
        "_run_isolated_child",
        lambda *_args: {"outcome": "worker_error", "errorType": "RuntimeError"},
    )

    with pytest.raises(HTTPException) as failed:
        await worker_app.process_document(_request(body), _grant(body))

    assert failed.value.status_code == 503
    assert failed.value.detail == {
        "error": "OCR_WORKER_UNAVAILABLE",
        "message": "isolated OCR processing is temporarily unavailable",
    }
    assert worker_app._active_job is False


@pytest.mark.anyio
async def test_worker_timeout_returns_gateway_timeout_and_releases_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"%PDF-1.7\n"
    monkeypatch.setenv("OCR_WORKER_GRANT_SECRET", SECRET)
    monkeypatch.setattr(worker_app, "_active_job", False)

    def timeout(*_args):
        raise TimeoutError("private parser diagnostic")

    monkeypatch.setattr(worker_app, "_run_isolated_child", timeout)
    with pytest.raises(HTTPException) as failed:
        await worker_app.process_document(_request(body), _grant(body))

    assert failed.value.status_code == 504
    assert failed.value.detail["error"] == "OCR_WORKER_TIMEOUT"
    assert "private parser diagnostic" not in str(failed.value.detail)
    assert worker_app._active_job is False
