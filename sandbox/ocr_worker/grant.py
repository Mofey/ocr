"""CMD-signed, short-lived capability for one immutable OCR input."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


MAX_GRANT_LIFETIME_SECONDS = 120
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORMAT_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class OCRInputGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audience: Literal["document-ocr-worker"]
    workspace_id: UUID = Field(alias="workspaceId")
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(alias="contentType", min_length=1, max_length=120)
    size_bytes: int = Field(alias="sizeBytes", gt=0, le=2 * 1024 * 1024)
    sha256: str = Field(min_length=64, max_length=64)
    issued_at: int = Field(alias="issuedAt", gt=0)
    expires_at: int = Field(alias="expiresAt", gt=0)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _SHA256.fullmatch(normalized):
            raise ValueError("sha256 must be a hexadecimal SHA-256 digest")
        return normalized

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "/" in normalized or "\\" in normalized or "\x00" in normalized:
            raise ValueError("filename must be a basename")
        return normalized

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, value: str, info) -> str:
        content_type = value.strip().lower()
        filename = str(info.data.get("filename", ""))
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        expected = _FORMAT_BY_EXTENSION.get(f".{extension}")
        if expected is None or content_type != expected:
            raise ValueError("only the bounded PDF/image OCR allowlist is accepted")
        return content_type

    def validate_lifetime(self, now: int | None = None) -> None:
        current = int(time.time()) if now is None else int(now)
        if self.expires_at <= current:
            raise ValueError("OCR job grant has expired")
        if self.issued_at > current + 30:
            raise ValueError("OCR job grant issue time is in the future")
        if self.expires_at <= self.issued_at or self.expires_at - self.issued_at > MAX_GRANT_LIFETIME_SECONDS:
            raise ValueError("OCR job grant lifetime exceeds policy")


class OCRJobGrant(OCRInputGrant):
    schema_version: Literal[1] = Field(alias="schemaVersion")
    job_id: UUID = Field(alias="jobId")
    document_id: UUID = Field(alias="documentId")
    version_id: UUID = Field(alias="versionId")
    object_key: str = Field(alias="objectKey", min_length=1, max_length=512)

    @field_validator("object_key")
    @classmethod
    def validate_immutable_key(cls, value: str, info):
        expected = f"immutable/workspaces/{info.data.get('workspace_id')}/documents/{info.data.get('document_id')}/versions/{info.data.get('version_id')}"
        if value != expected:
            raise ValueError("objectKey does not match the granted immutable version")
        return value


class TransientReceiptGrant(OCRInputGrant):
    # A preview capability cannot be used as an immutable Files capability.
    schema_version: Literal[2] = Field(alias="schemaVersion")
    purpose: Literal["receipt-preview"]
    request_id: UUID = Field(alias="requestId")


class OCRJobGrantError(ValueError):
    pass


def _decode_base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_ocr_job_grant(token: str, secret: str | None = None, now: int | None = None) -> OCRJobGrant | TransientReceiptGrant:
    signing_secret = secret if secret is not None else os.getenv("OCR_WORKER_GRANT_SECRET", "")
    if len(signing_secret.encode("utf-8")) < 32:
        raise OCRJobGrantError("OCR worker grant verification is not configured")
    if not token or len(token) > 4096 or token.count(".") != 1:
        raise OCRJobGrantError("OCR job grant is malformed")

    encoded_payload, encoded_signature = token.split(".", 1)
    try:
        payload = _decode_base64url(encoded_payload)
        supplied_signature = _decode_base64url(encoded_signature)
    except (ValueError, base64.binascii.Error) as error:
        raise OCRJobGrantError("OCR job grant is malformed") from error

    expected_signature = hmac.new(signing_secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise OCRJobGrantError("OCR job grant signature is invalid")

    try:
        claims = json.loads(payload)
        grant_type = TransientReceiptGrant if isinstance(claims, dict) and claims.get("schemaVersion") == 2 else OCRJobGrant
        grant = grant_type.model_validate(claims)
        grant.validate_lifetime(now)
    except (ValidationError, ValueError) as error:
        raise OCRJobGrantError("OCR job grant claims are invalid") from error
    return grant


def encode_test_grant(payload: dict, secret: str) -> str:
    """Deterministic encoder used by contract tests, never used for runtime grants."""
    if len(secret.encode("utf-8")) < 32:
        raise ValueError("test secret must contain at least 32 bytes")
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    encoded_payload = base64.urlsafe_b64encode(serialized).rstrip(b"=").decode("ascii")
    signature = hmac.new(secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{encoded_payload}.{encoded_signature}"
