"""Bounded, tenant-scoped immutable bytes; PostgreSQL owns references and GC."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
from contextlib import contextmanager
from threading import BoundedSemaphore
from urllib.parse import urlsplit

import botocore.session
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, InvalidRegionError

from .errors import CoreError
from .security import validate_http_url


class S3BlobStore:
    def __init__(self, *, endpoint_url, bucket, region, access_key_id,
                 secret_access_key, tenant_id, session_token=None, prefix="core-agent",
                 max_bytes=100_000_000, max_inflight=4):
        endpoint = validate_http_url(endpoint_url, "S3_ENDPOINT_URL")
        try:
            loopback = endpoint.hostname == "localhost" or ipaddress.ip_address(endpoint.hostname).is_loopback
        except ValueError:
            loopback = False
        if (endpoint.username is not None or endpoint.password is not None
                or endpoint.query or endpoint.fragment or urlsplit(endpoint_url).path not in {"", "/"}
                or (endpoint.scheme != "https" and not loopback)):
            raise CoreError("CONFIG_INVALID", "S3_ENDPOINT_URL requires an HTTPS origin without credentials, path, query or fragment (HTTP allowed only on loopback)")
        for name, value in (("BUCKET", bucket), ("REGION", region), ("ACCESS_KEY_ID", access_key_id),
                            ("SECRET_ACCESS_KEY", secret_access_key), ("TENANT_ID", tenant_id), ("PREFIX", prefix)):
            if not isinstance(value, str) or not value:
                raise CoreError("CONFIG_INVALID", f"S3_{name} must be a non-empty string")
        if re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) is None:
            raise CoreError("CONFIG_INVALID", "S3_BUCKET must contain 3-63 lowercase letters, digits, dots or hyphens, starting and ending with a letter or digit")
        if len(prefix) > 512 or any(re.fullmatch(r"[A-Za-z0-9_-]+", part) is None for part in prefix.split("/")):
            raise CoreError("CONFIG_INVALID", "S3_PREFIX requires slash-separated letters, digits, underscores or hyphens, at most 512 characters")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 2_147_483_647:
            raise CoreError("CONFIG_INVALID", "S3 max_bytes must be an integer between 1 and 2147483647")
        if type(max_inflight) is not int or not 1 <= max_inflight <= 32:
            raise CoreError("CONFIG_INVALID", "S3 max_inflight must be an integer between 1 and 32")
        if session_token is not None and (not isinstance(session_token, str) or not session_token):
            raise CoreError("CONFIG_INVALID", "S3_SESSION_TOKEN must be a non-empty string when supplied")
        self.tenant_id = tenant_id
        self.bucket = bucket
        self.prefix = f"{prefix}/tenants/{hashlib.sha256(tenant_id.encode()).hexdigest()}/blobs/sha256"
        self.max_bytes = max_bytes
        self._slots = BoundedSemaphore(max_inflight)
        try:
            self.client = botocore.session.get_session().create_client(
                "s3", endpoint_url=endpoint_url, region_name=region,
                aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key,
                aws_session_token=session_token,
                config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=30,
                    max_pool_connections=max_inflight, retries={"total_max_attempts": 1},
                    s3={"addressing_style": "path"},
                    request_checksum_calculation="when_required", response_checksum_validation="when_required"),
            )
        except InvalidRegionError:
            raise CoreError("CONFIG_INVALID", "S3_REGION is not a valid SDK region name") from None
        except (BotoCoreError, ValueError) as error:
            raise CoreError("CONFIG_INVALID", f"S3 client initialization failed ({type(error).__name__})") from None

    def key(self, digest, *, object_id=None):
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        if object_id is not None and (not isinstance(object_id, str) or re.fullmatch(r"[0-9a-f]{32}", object_id) is None):
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        return f"{self.prefix}/{digest}" + (f"/{object_id}" if object_id is not None else "")

    @contextmanager
    def _slot(self):
        if not self._slots.acquire(timeout=30):
            raise CoreError("STORAGE_UNAVAILABLE", retryable=True)
        try:
            yield
        except (ClientError, BotoCoreError, OSError):
            raise CoreError("STORAGE_UNAVAILABLE", retryable=True) from None
        finally:
            self._slots.release()

    @staticmethod
    def _missing(error):
        return error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}

    def _read(self, digest, size, *, object_id=None):
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key(digest, object_id=object_id))
        except ClientError as error:
            if self._missing(error):
                raise CoreError("NOT_FOUND") from None
            raise
        body = response["Body"]
        try:
            if response.get("ContentLength") != size:
                raise CoreError("ARTIFACT_INTEGRITY_FAILED")
            content = body.read(size + 1)
        finally:
            body.close()
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        return content

    def _exists_verified(self, digest, size, *, object_id=None):
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=self.key(digest, object_id=object_id), ChecksumMode="ENABLED")
        except ClientError as error:
            if self._missing(error):
                return False
            raise
        if response.get("ContentLength") != size:
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        checksum = response.get("ChecksumSHA256")
        if checksum is None:
            self._read(digest, size, object_id=object_id)
        elif checksum != base64.b64encode(bytes.fromhex(digest)).decode():
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        return True

    def put(self, content, *, object_id=None):
        if not isinstance(content, bytes):
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        if len(content) > self.max_bytes:
            raise CoreError("ARTIFACT_TOO_LARGE")
        digest = hashlib.sha256(content).hexdigest()
        with self._slot():
            if self._exists_verified(digest, len(content), object_id=object_id):
                return digest
            try:
                self.client.put_object(Bucket=self.bucket, Key=self.key(digest, object_id=object_id), Body=content,
                    ContentLength=len(content), IfNoneMatch="*",
                    ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode())
            except (ClientError, BotoCoreError, OSError):
                # An ambiguous PUT or a concurrent writer is reconciled at the same key.
                if self._exists_verified(digest, len(content), object_id=object_id):
                    return digest
                raise CoreError("STORAGE_UNAVAILABLE", retryable=True) from None
            if not self._exists_verified(digest, len(content), object_id=object_id):
                raise CoreError("STORAGE_UNAVAILABLE", retryable=True)
        return digest

    def get(self, digest, size, *, object_id=None):
        self.key(digest, object_id=object_id)
        if type(size) is not int or not 0 <= size <= self.max_bytes:
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        with self._slot():
            return self._read(digest, size, object_id=object_id)

    def delete(self, digest, *, object_id=None):
        """Only a fenced PostgreSQL GC decision may authorize this operation."""
        key = self.key(digest, object_id=object_id)
        with self._slot():
            self.client.delete_object(Bucket=self.bucket, Key=key)

    def check(self):
        with self._slot():
            self.client.head_bucket(Bucket=self.bucket)

    def close(self):
        self.client.close()
