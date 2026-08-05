from __future__ import annotations

import hashlib
import hmac
import logging
import json
import mimetypes
import re
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .errors import CoreError

CLOUD_RU_ENDPOINT = "https://s3.cloud.ru"
MAX_NAME_LENGTH = 512
MAX_METADATA_BYTES = 2048
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f/\\]")
_METADATA_KEY = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_BUCKET = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,254}\Z")
_REGION = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9-]{0,62}\Z")
_WHITESPACE = re.compile(r"\s+")


# A slim image ships no /etc/mime.types, so `mimetypes` falls back to its
# built-in table — which has no OOXML. Without these, every .pptx the agent
# produces is labelled text/plain, and a client that believes the label decodes
# a ZIP container as text.
for _extension, _media_type in {
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".epub": "application/epub+zip",
    ".rtf": "application/rtf",
    ".7z": "application/x-7z-compressed",
    ".webp": "image/webp",
}.items():
    mimetypes.add_type(_media_type, _extension)


def guess_media_type(filename, *, fallback="text/plain"):
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or fallback


def _content_disposition(filename):
    """Name the download, because the object key ends in the version number.

    Without this the last segment of the object URL is `0`, so the file arrives
    called `0` with no extension: no application opens it, and a client that
    decides inline-versus-download by name may never offer to save it at all.
    """
    # Header values are ASCII; RFC 5987 carries the real name beside a fallback.
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    disposition = f'attachment; filename="{ascii_name}"'
    if not filename.isascii():
        disposition += f"; filename*=UTF-8''{quote(filename, safe='')}"
    return disposition



def validate_segment(value, label):
    """Validate one path segment of an artifact key (trust boundary)."""
    if not isinstance(value, str) or not value:
        raise CoreError("TOOL_ARGUMENT_INVALID", f"{label} must be a non-empty string")
    if len(value) > MAX_NAME_LENGTH:
        raise CoreError("TOOL_ARGUMENT_INVALID", f"{label} is too long")
    # "." and ".." are rejected because HTTP intermediaries may collapse such a
    # segment out of the signed request path and hit a different object.
    if value in (".", "..") or ".." in value or _FORBIDDEN.search(value):
        raise CoreError("TOOL_ARGUMENT_INVALID", f"{label} has an unsupported character")
    return value


def _validate_version(version):
    """Validate a caller-supplied version number (trust boundary)."""
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise CoreError("TOOL_ARGUMENT_INVALID", "version must be a non-negative integer")
    return version


def _is_version_segment(tail):
    """Only canonical decimal keys map back to the key this service writes."""
    return tail.isascii() and tail.isdigit() and (tail == "0" or not tail.startswith("0"))


def _validate_media_type(media_type):
    if not isinstance(media_type, str) or not media_type:
        raise CoreError("TOOL_ARGUMENT_INVALID", "media type must be a non-empty string")
    if len(media_type) > 255 or not media_type.isascii() or _CONTROL.search(media_type):
        raise CoreError("TOOL_ARGUMENT_INVALID", "media type is not a valid header value")
    return media_type


def _encode_metadata(metadata):
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict) or any(
        not isinstance(key, str) for key in metadata
    ):
        raise CoreError("TOOL_ARGUMENT_INVALID", "metadata must be a dict with str keys")
    try:
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise CoreError("TOOL_ARGUMENT_INVALID", "metadata must be JSON serializable") from None
    if len(encoded.encode()) > MAX_METADATA_BYTES:
        raise CoreError("TOOL_ARGUMENT_INVALID", "metadata is too large")
    return encoded


@dataclass(frozen=True)
class ArtifactVersion:
    filename: str
    scope: str
    version: int
    media_type: str
    size: int
    digest: str
    metadata: dict = field(default_factory=dict)


class ArtifactBackend:
    """Key/value contract every artifact backend implements.

    ``media_type`` is only a native content-type hint for the backend; a caller
    that needs it back on ``get`` must also record it inside ``metadata``.
    """

    def put(self, key, content, *, media_type, metadata, filename=None):
        raise NotImplementedError

    def get(self, key):
        raise NotImplementedError

    def list_prefix(self, prefix):
        raise NotImplementedError



class InMemoryArtifactBackend(ArtifactBackend):
    def __init__(self):
        self._objects = {}

    def put(self, key, content, *, media_type, metadata, filename=None):
        self._objects[key] = (bytes(content), dict(metadata))

    def get(self, key):
        try:
            content, metadata = self._objects[key]
        except KeyError:
            raise CoreError("NOT_FOUND") from None
        return content, dict(metadata)

    def list_prefix(self, prefix):
        return sorted(key for key in self._objects if key.startswith(prefix))



class ArtifactService:
    """Named, scoped and versioned artifacts on top of any ArtifactBackend."""

    def __init__(self, backend, *, max_bytes=100_000_000):
        if backend is None:
            raise CoreError("CONFIG_INVALID", "artifact backend is required")
        self.backend = backend
        self.max_bytes = int(max_bytes)
        if self.max_bytes <= 0:
            raise CoreError("CONFIG_INVALID", "max_bytes must be positive")

    def _scope(self, filename):
        if not isinstance(filename, str):
            raise CoreError("TOOL_ARGUMENT_INVALID", "filename must be a string")
        if filename.startswith("user:"):
            return "user", filename[len("user:") :]
        return "session", filename

    def _session_root(self, app_name, user_id, session_id):
        validate_segment(app_name, "app_name")
        validate_segment(user_id, "user_id")
        if not session_id:
            raise CoreError(
                "CONFIG_INVALID", "session id required for session-scoped artifacts"
            )
        validate_segment(session_id, "session_id")
        return f"apps/{app_name}/users/{user_id}/sessions/{session_id}/artifacts/"

    def _user_root(self, app_name, user_id):
        validate_segment(app_name, "app_name")
        validate_segment(user_id, "user_id")
        return f"apps/{app_name}/users/{user_id}/artifacts/"

    def _versions_prefix(self, app_name, user_id, session_id, filename):
        scope, name = self._scope(filename)
        validate_segment(name, "filename")
        if scope == "user":
            root = self._user_root(app_name, user_id)
        else:
            root = self._session_root(app_name, user_id, session_id)
        return scope, name, f"{root}{name}/versions/"

    def save(
        self,
        *,
        app_name,
        user_id,
        session_id,
        filename,
        content,
        media_type=None,
        metadata=None,
    ):
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise CoreError("TOOL_ARGUMENT_INVALID", "content must be bytes")
        content = bytes(content)
        if len(content) > self.max_bytes:
            raise CoreError("ARTIFACT_TOO_LARGE")
        scope, name, prefix = self._versions_prefix(app_name, user_id, session_id, filename)
        media_type = _validate_media_type(media_type or guess_media_type(name))
        encoded_metadata = _encode_metadata(metadata)
        existing = self._versions(prefix)
        version = len(existing)
        if existing and version <= existing[-1]:
            # A deleted middle version must never let a later save overwrite one.
            version = existing[-1] + 1
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        stored = {
            "digest": digest,
            "size": str(len(content)),
            "media-type": media_type,
            "metadata": encoded_metadata,
        }
        self.backend.put(
            f"{prefix}{version}",
            content,
            media_type=media_type,
            metadata=stored,
            filename=name,
        )
        return ArtifactVersion(
            filename=name,
            scope=scope,
            version=version,
            media_type=media_type,
            size=len(content),
            digest=digest,
            metadata=json.loads(encoded_metadata),
        )

    def load(self, *, app_name, user_id, session_id, filename, version=None):
        scope, name, prefix = self._versions_prefix(app_name, user_id, session_id, filename)
        if version is None:
            existing = self._versions(prefix)
            if not existing:
                raise CoreError("NOT_FOUND")
            version = existing[-1]
        else:
            version = _validate_version(version)
        content, stored = self.backend.get(f"{prefix}{version}")
        content = bytes(content)
        if "sha256:" + hashlib.sha256(content).hexdigest() != stored.get("digest"):
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        try:
            size = int(stored.get("size", ""))
            metadata = json.loads(stored.get("metadata", "{}"))
        except (TypeError, ValueError):
            raise CoreError("ARTIFACT_INTEGRITY_FAILED") from None
        if size != len(content) or not isinstance(metadata, dict):
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        return (
            ArtifactVersion(
                filename=name,
                scope=scope,
                version=version,
                media_type=stored.get("media-type") or guess_media_type(name),
                size=size,
                digest=stored["digest"],
                metadata=metadata,
            ),
            content,
        )

    def list_keys(self, *, app_name, user_id, session_id):
        """Return (session filenames, user filenames with the "user:" prefix)."""
        user_root = self._user_root(app_name, user_id)
        user_names = [f"user:{name}" for name in self._names(user_root)]
        if not session_id:
            return [], user_names
        session_root = self._session_root(app_name, user_id, session_id)
        return self._names(session_root), user_names


    def _names(self, root):
        names = set()
        for key in self.backend.list_prefix(root):
            name = key[len(root) :].split("/", 1)[0]
            if name:
                names.add(name)
        return sorted(names)

    def _versions(self, prefix):
        versions = []
        for key in self.backend.list_prefix(prefix):
            tail = key[len(prefix) :]
            if _is_version_segment(tail):
                versions.append(int(tail))
        return sorted(versions)


def _sigv4_key(secret_access_key, datestamp, region):
    key = f"AWS4{secret_access_key}".encode()
    for value in (datestamp, region, "s3", "aws4_request"):
        key = hmac.new(key, value.encode(), hashlib.sha256).digest()
    return key


class S3ArtifactBackend(ArtifactBackend):
    """AWS S3 / MinIO / Cloud.ru integration signed with stdlib SigV4."""

    def __init__(
        self,
        *,
        bucket,
        region,
        access_key_id,
        secret_access_key,
        endpoint_url=None,
        tenant_id=None,
        connect_timeout=60.0,
        read_timeout=300.0,
        max_attempts=1,
        retry_initial_delay=1.0,
        retry_max_delay=60.0,
        retry_max_total_seconds=0.0,
    ):
        for value, label in (
            (bucket, "bucket"),
            (region, "region"),
            (access_key_id, "access_key_id"),
            (secret_access_key, "secret_access_key"),
        ):
            if not isinstance(value, str) or not value:
                raise CoreError("CONFIG_INVALID", f"s3 {label} is required")
        # bucket and region are interpolated into the virtual-host name and the
        # credential scope, so an unvalidated value redirects signed requests.
        if not _BUCKET.match(bucket):
            raise CoreError("CONFIG_INVALID", "s3 bucket name is not valid")
        if not _REGION.match(region):
            raise CoreError("CONFIG_INVALID", "s3 region is not valid")
        if tenant_id is not None and (not isinstance(tenant_id, str) or not tenant_id):
            raise CoreError("CONFIG_INVALID", "s3 tenant_id must be a non-empty string")
        credential = f"{tenant_id}:{access_key_id}" if tenant_id else access_key_id
        if not credential.isascii() or _CONTROL.search(credential):
            raise CoreError("CONFIG_INVALID", "s3 access_key_id is not header safe")
        tenant, _, key = credential.partition(":")
        if endpoint_url and not (credential.count(":") == 1 and tenant and key):
            # Cloud.ru authenticates as "tenant_id:key_id"; a bare key would only
            # surface as a 403 on the first write.
            raise CoreError(
                "CONFIG_INVALID",
                "s3 access key must be tenant_id:key_id; set ARTIFACT_S3_TENANT_ID "
                "or put both parts in ARTIFACT_S3_ACCESS_KEY_ID",
            )
        self.bucket = bucket
        self.region = region
        self.access_key_id = credential
        self._secret_access_key = secret_access_key
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        # urllib exposes a single socket timeout for connect and read.
        self._timeout = max(self.connect_timeout, self.read_timeout)
        self.max_attempts = max(1, int(max_attempts))
        self.retry_initial_delay = float(retry_initial_delay)
        self.retry_max_delay = float(retry_max_delay)
        self.retry_max_total_seconds = float(retry_max_total_seconds)
        if endpoint_url:
            # This backend targets Cloud.ru Object Storage, so a typo must not take
            # the deployment down. The warning has to name both values: the request
            # goes to a host the operator did not write, carrying artifact bytes
            # and the access key id.
            if endpoint_url.rstrip("/") != CLOUD_RU_ENDPOINT:
                logging.getLogger("core_agent.runtime").warning(
                    "ignoring ARTIFACT_S3_ENDPOINT_URL %r; using %s",
                    endpoint_url,
                    CLOUD_RU_ENDPOINT,
                )
            host = urlparse(CLOUD_RU_ENDPOINT).hostname
            self._base = CLOUD_RU_ENDPOINT
            self._key_root = f"/{quote(bucket, safe='')}"
        else:
            host = f"{bucket}.s3.{region}.amazonaws.com"
            self._base = f"https://{host}"
            self._key_root = ""
        self._host = host

    def _path(self, key):
        return f"{self._key_root}/{quote(key, safe='/~')}"

    def _sign(self, method, path, canonical_query, body, headers):
        stamp = time.gmtime()
        amz_date = time.strftime("%Y%m%dT%H%M%SZ", stamp)
        datestamp = time.strftime("%Y%m%d", stamp)
        payload_hash = hashlib.sha256(body).hexdigest()
        # SigV4 canonicalises header values by trimming and collapsing runs of
        # whitespace, so the value sent must already be in that form.
        signed = {
            name.lower(): _WHITESPACE.sub(" ", str(value)).strip()
            for name, value in headers.items()
        }
        signed["host"] = self._host
        signed["x-amz-date"] = amz_date
        signed["x-amz-content-sha256"] = payload_hash
        names = sorted(signed)
        canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in names)
        signed_names = ";".join(names)
        canonical_request = "\n".join(
            (
                method,
                path or "/",
                canonical_query,
                canonical_headers,
                signed_names,
                payload_hash,
            )
        )
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            (
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            )
        )
        signature = hmac.new(
            _sigv4_key(self._secret_access_key, datestamp, self.region),
            string_to_sign.encode(),
            hashlib.sha256,
        ).hexdigest()
        signed["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key_id}/{scope}, "
            f"SignedHeaders={signed_names}, Signature={signature}"
        )
        return signed

    def _request(self, method, path, *, query=None, body=b"", headers=None):
        canonical_query = "&".join(
            f"{quote(name, safe='~')}={quote(str(value), safe='~')}"
            for name, value in sorted((query or {}).items())
        )
        url = self._base + (path or "/")
        if canonical_query:
            url = f"{url}?{canonical_query}"
        request = Request(
            url,
            data=body if method in ("PUT", "POST") else None,
            headers=self._sign(method, path or "/", canonical_query, body, headers or {}),
            method=method,
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return response.read(), list(response.headers.items())
        except HTTPError as error:
            error.close()
            if error.code == 404:
                raise CoreError("NOT_FOUND") from None
            raise CoreError(
                "ARTIFACT_BACKEND_UNAVAILABLE",
                f"s3 responded with status {error.code}",
                retryable=error.code >= 500,
            ) from None
        except (URLError, TimeoutError, OSError) as error:
            raise CoreError(
                "ARTIFACT_BACKEND_UNAVAILABLE", "s3 request failed", retryable=True
            ) from error

    def _idempotent(self, method, path, *, query=None):
        started = time.monotonic()
        delay = self.retry_initial_delay
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._request(method, path, query=query)
            except CoreError as error:
                budget = self.retry_max_total_seconds
                exhausted = budget > 0 and delay > budget - (time.monotonic() - started)
                if not error.retryable or attempt >= self.max_attempts or exhausted:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, self.retry_max_delay)
        raise CoreError("ARTIFACT_BACKEND_UNAVAILABLE", "s3 retries exhausted", retryable=True)

    def put(self, key, content, *, media_type, metadata, filename=None):
        headers = {"content-type": _validate_media_type(media_type)}
        if filename:
            headers["content-disposition"] = _content_disposition(filename)
        for name, value in metadata.items():
            if not _METADATA_KEY.match(name):
                raise CoreError("TOOL_ARGUMENT_INVALID", "metadata key is not header safe")
            value = str(value)
            if not value.isascii() or _CONTROL.search(value):
                raise CoreError("TOOL_ARGUMENT_INVALID", "metadata value is not header safe")
            headers[f"x-amz-meta-{name.lower()}"] = value
        self._request("PUT", self._path(key), body=bytes(content), headers=headers)

    def get(self, key):
        body, headers = self._idempotent("GET", self._path(key))
        metadata = {}
        for name, value in headers:
            lowered = name.lower()
            if lowered.startswith("x-amz-meta-"):
                metadata[lowered[len("x-amz-meta-") :]] = value
        return body, metadata

    def list_prefix(self, prefix):
        keys = []
        token = None
        while True:
            query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                query["continuation-token"] = token
            body, _ = self._idempotent("GET", self._key_root or "/", query=query)
            try:
                root = ElementTree.fromstring(body)
            except ElementTree.ParseError:
                raise CoreError(
                    "ARTIFACT_BACKEND_UNAVAILABLE", "s3 returned an unparsable listing"
                ) from None
            keys.extend(
                element.text
                for element in root.iterfind("{*}Contents/{*}Key")
                if element.text
            )
            token = root.findtext("{*}NextContinuationToken")
            if not token:
                return sorted(keys)



class MongoDbArtifactBackend(ArtifactBackend):
    """MongoDB integration: one document per key, no index or schema management."""

    def __init__(self, *, collection=None, url=None, database=None, collection_name="artifacts"):
        try:
            from bson.binary import Binary
            from pymongo import MongoClient
        except ImportError:
            Binary = None
            MongoClient = None
        if collection is None:
            if MongoClient is None:
                raise CoreError(
                    "CONFIG_INVALID", "mongodb artifact storage requires pymongo"
                )
            if not isinstance(url, str) or not url:
                raise CoreError("CONFIG_INVALID", "mongodb url is required")
            if not isinstance(database, str) or not database:
                raise CoreError("CONFIG_INVALID", "mongodb database is required")
            if not isinstance(collection_name, str) or not collection_name:
                raise CoreError("CONFIG_INVALID", "mongodb collection is required")
            collection = MongoClient(url)[database][collection_name]
        self.collection = collection
        # An injected collection (tests) may run without pymongo installed.
        self._binary = Binary or bytes

    def put(self, key, content, *, media_type, metadata, filename=None):
        self.collection.replace_one(
            {"_id": key},
            {
                "_id": key,
                "content": self._binary(bytes(content)),
                "media_type": _validate_media_type(media_type),
                "metadata": dict(metadata),
            },
            upsert=True,
        )

    def get(self, key):
        document = self.collection.find_one({"_id": key})
        if not document:
            raise CoreError("NOT_FOUND")
        return bytes(document.get("content") or b""), dict(document.get("metadata") or {})

    def list_prefix(self, prefix):
        cursor = self.collection.find(
            {"_id": {"$regex": f"^{re.escape(prefix)}"}}, {"_id": 1}
        )
        return sorted(str(document["_id"]) for document in cursor)


def create_artifact_service(
    *,
    storage_type="in-memory",
    max_bytes=100_000_000,
    s3_bucket=None,
    s3_region="",
    s3_access_key_id=None,
    s3_secret_access_key=None,
    s3_endpoint_url=None,
    s3_tenant_id=None,
    s3_connect_timeout=60.0,
    s3_read_timeout=300.0,
    s3_max_attempts=1,
    s3_retry_initial_delay=1.0,
    s3_retry_max_delay=60.0,
    s3_retry_max_total_seconds=0.0,
    mongodb_url=None,
    mongodb_database="artifacts",
    mongodb_collection="artifacts",
):
    """Build an ArtifactService from explicit configuration values."""
    if storage_type == "in-memory":
        return ArtifactService(InMemoryArtifactBackend(), max_bytes=max_bytes)
    if storage_type == "s3":
        return ArtifactService(
            S3ArtifactBackend(
                bucket=s3_bucket,
                region=s3_region,
                access_key_id=s3_access_key_id,
                secret_access_key=s3_secret_access_key,
                endpoint_url=s3_endpoint_url,
                tenant_id=s3_tenant_id,
                connect_timeout=s3_connect_timeout,
                read_timeout=s3_read_timeout,
                max_attempts=s3_max_attempts,
                retry_initial_delay=s3_retry_initial_delay,
                retry_max_delay=s3_retry_max_delay,
                retry_max_total_seconds=s3_retry_max_total_seconds,
            ),
            max_bytes=max_bytes,
        )
    if storage_type == "mongodb":
        return ArtifactService(
            MongoDbArtifactBackend(
                url=mongodb_url,
                database=mongodb_database,
                collection_name=mongodb_collection,
            ),
            max_bytes=max_bytes,
        )
    raise CoreError("CONFIG_INVALID", "unsupported artifact storage type")
