from __future__ import annotations

import base64
import ipaddress
import json
import math
import re
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from a2a.types.a2a_pb2 import SendMessageResponse, Task
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError

from .a2a_input import _check_depth, _decode_raw, _invalid_constant, _unique_object
from .errors import CoreError, ExecutionNotStarted
from .security import redact

AGENT_CARD_WELL_KNOWN_PATH = "/.well-known/agent-card.json"
A2A_PROTOCOL_VERSION = "1.0"
A2A_BINDING = "JSONRPC"
FORWARDED_CLIENT_HEADERS = ("Authorization", "X-PROJECT-ID", "X-A2A-Extensions")

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_FILE_RESPONSE_BYTES = 40_000_000
MAX_STREAM_FRAMES = 10_000
MAX_ERROR_MESSAGE_CHARS = 200
STREAM_DONE_SENTINEL = "[DONE]"

# 4xx are permanent except these two, which the peer itself marks as retryable.
RETRYABLE_CLIENT_STATUS_CODES = (408, 429)
# A 1.0 stream ends on a terminal or interrupted state; there is no `final` flag.
TERMINAL_TASK_STATES = frozenset(
    {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
        "TASK_STATE_INPUT_REQUIRED",
        "TASK_STATE_AUTH_REQUIRED",
    }
)


class _RefuseRedirects(HTTPRedirectHandler):
    """Redirects would resend the forwarded Authorization to an unvalidated host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Built once: every outbound call must go through the redirect-refusing opener.
_OPENER = build_opener(_RefuseRedirects())


@dataclass(frozen=True)
class RemoteAgentCard:
    name: str
    description: str
    url: str
    streaming: bool
    skills: tuple[dict, ...]
    binding: str = A2A_BINDING


@dataclass(frozen=True)
class RemoteEvent:
    kind: str
    state: str | None
    text: str
    final: bool
    parts: tuple[dict, ...]
    task_id: str | None = None
    context_id: str | None = None
    messages: tuple[dict, ...] = ()
    history_truncated: bool = False
    public_messages_complete: bool = False

    @property
    def has_files(self):
        return any("raw" in part or "url" in part for part in self.parts)


def _trusted_text(value, *, code="REMOTE_AGENT_DENIED"):
    if not isinstance(value, str) or not value or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value):
        raise CoreError(code)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise CoreError(code) from None
    return value


def _trusted_endpoint(url):
    _trusted_text(url)
    try:
        parsed = _validate_endpoint(url)
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or "?" in url or "#" in url or any(char.isspace() for char in url)
                or "\\" in parsed.netloc or parsed.netloc.endswith(":")
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError()
        return urlsplit(url)
    except (CoreError, ValueError):
        raise CoreError("REMOTE_AGENT_DENIED") from None


def _trusted_headers(headers):
    if headers is None:
        return {}
    if not isinstance(headers, Mapping):
        raise CoreError("REMOTE_AGENT_DENIED")
    reserved = {"host", "content-type", "content-length", "connection", "transfer-encoding",
                "upgrade", "trailer", "te", "proxy-authorization", "accept", "a2a-version"}
    result, names = {}, set()
    for name, value in headers.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", name)
                or name.lower() in reserved | names):
            raise CoreError("REMOTE_AGENT_DENIED")
        _trusted_text(value)
        try:
            if len(value.encode("latin-1")) > 16384:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise CoreError("REMOTE_AGENT_DENIED") from None
        names.add(name.lower())
        result[name] = value
    return result


def _request_timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise CoreError("CONFIG_INVALID", "remote request timeout must be finite and positive")
    return value


def _attachment_limit(value):
    if value is not None and (type(value) is not int or not 1 <= value <= 2147483647):
        raise CoreError("CONFIG_INVALID")
    return value


def _outgoing_parts(files, limit):
    if type(files) not in (tuple, list):
        raise CoreError("INVALID_FILE_INPUT")
    if files and limit is None:
        raise CoreError("CONFIG_INVALID")
    total = 0
    for file in files:
        if type(file) is not dict or file.keys() != {"name", "media_type", "raw"} or type(file["raw"]) is not bytes:
            raise CoreError("INVALID_FILE_INPUT")
        name = _trusted_text(file["name"], code="INVALID_FILE_INPUT")
        _trusted_text(file["media_type"], code="INVALID_FILE_INPUT")
        if name in {".", ".."} or "/" in name or "\\" in name:
            raise CoreError("INVALID_FILE_INPUT")
        total += len(file["raw"])
    if limit is not None and total > limit:
        raise CoreError("ATTACHMENTS_TOO_LARGE", data={"allowed_bytes": limit, "actual_bytes": total})
    return [{"filename": file["name"], "mediaType": file["media_type"],
             "raw": base64.b64encode(file["raw"]).decode("ascii")} for file in files]


def _validate_response_files(payload, *, direct, limit):
    """Validate every known Part collection before protobuf decodes file bytes."""
    if not isinstance(payload, dict):
        return  # The SDK validates the response schema.
    containers = [payload] if direct else [payload.get("task"), payload.get("message")]
    total = 0
    try:
        for container in containers:
            if not isinstance(container, dict):
                continue
            collections = [(container, True)]
            status = container.get("status")
            if isinstance(status, dict):
                collections.append((status.get("message"), True))
            for key in ("artifacts", "history"):
                items = container.get(key)
                if isinstance(items, list):
                    collections.extend((item, key != "history") for item in items)
            for message, current in collections:
                if message is None:
                    continue
                if not isinstance(message, dict) or not isinstance(message.get("parts", []), list):
                    raise ValueError()
                for part in message.get("parts", []):
                    if (not isinstance(part, dict) or len(part.keys() & {"text", "raw", "url", "data"}) != 1
                            or part.keys() - {"text", "raw", "url", "data", "filename", "mediaType", "media_type", "metadata"}):
                        raise ValueError()
                    for field in ("text", "filename", "mediaType", "media_type"):
                        if field in part:
                            if not isinstance(part[field], str):
                                raise ValueError()
                            part[field].encode("utf-8")
                    if "url" in part or any(field in part and not isinstance(part[field], dict) for field in ("data", "metadata")):
                        raise ValueError()
                    if "raw" in part:
                        size = len(_decode_raw(part["raw"]))
                        if current:
                            total += size
        if limit is not None and total > limit:
            raise CoreError("ATTACHMENTS_TOO_LARGE", data={"allowed_bytes": limit, "actual_bytes": total})
    except (ValueError, TypeError):
        raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR") from None


def _task_event(payload, *, direct=False, expected_task_id=None, attachment_limit_bytes=None):
    """Validate the installed SDK's 1.0 shape without publishing parser diagnostics."""
    _validate_response_files(payload, direct=direct, limit=attachment_limit_bytes)
    try:
        parsed = ParseDict(payload, Task() if direct else SendMessageResponse())
        if direct or parsed.WhichOneof("payload") == "task":
            task = parsed if direct else parsed.task
            _trusted_text(task.id, code="REMOTE_AGENT_PROTOCOL_ERROR")
            _trusted_text(task.context_id, code="REMOTE_AGENT_PROTOCOL_ERROR")
            if expected_task_id is not None and task.id != expected_task_id:
                raise ValueError()
            if task.status.state not in range(1, 9):
                raise ValueError()
            raw = MessageToDict(task)
            parts = list(task.status.message.parts)
            for artifact in task.artifacts:
                parts.extend(artifact.parts)
            state = raw["status"]["state"]
            task_id, context_id, kind = task.id, task.context_id, "task"
            final = state in {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
        elif parsed.WhichOneof("payload") == "message":
            message = parsed.message
            _trusted_text(message.message_id, code="REMOTE_AGENT_PROTOCOL_ERROR")
            if message.role != 2 or not message.parts:
                raise ValueError()
            parts = message.parts
            for identifier in (message.task_id, message.context_id):
                if identifier:
                    _trusted_text(identifier, code="REMOTE_AGENT_PROTOCOL_ERROR")
            task_id, context_id, kind, state, final = message.task_id or None, message.context_id or None, "message", None, True
        else:
            raise ValueError()
        if any(part.WhichOneof("content") is None for part in parts):
            raise ValueError()
        normalized = tuple(MessageToDict(part) for part in parts)
        from .peer_conversations import MAX_MESSAGES, MAX_TEXT_BYTES, public_identity
        messages, seen, used, truncated = [], set(), 0, False

        def publish(container, identity):
            nonlocal used, truncated
            metadata = container.get("metadata", {})
            if isinstance(metadata, dict) and (
                any(metadata.get(key) is True for key in (
                    "private", "review_private", "adk_thought", "reasoning", "thinking",
                    "reasoning_replay", "provider_replay"))
                or metadata.get("visibility") == "private"
                or metadata.get("kind") in {"reasoning", "thinking", "review", "replay", "reasoning_replay", "provider_replay"}
            ):
                return
            public_parts = []
            for part in _parts(container):
                metadata = part.get("metadata", {})
                if isinstance(metadata, dict) and (
                    any(metadata.get(key) for key in (
                        "private", "reasoning", "thinking", "review_private", "adk_thought",
                        "reasoning_replay", "provider_replay"))
                    or metadata.get("visibility") == "private"
                    or metadata.get("kind") in {"reasoning", "thinking", "review", "replay", "reasoning_replay", "provider_replay"}
                ):
                    continue
                if isinstance(part.get("text"), str):
                    public_parts.append(part)
            text = _parts_text(public_parts)
            if not text:
                return
            identifier = public_identity(identity, text)
            if identifier in seen:
                return
            size = len(text.encode("utf-8"))
            if len(messages) >= MAX_MESSAGES or used + size > MAX_TEXT_BYTES:
                truncated = True
                return
            seen.add(identifier)
            used += size
            messages.append({"id": identifier, "text": text})

        if kind == "task":
            public_task = MessageToDict(task)
            status_message = public_task.get("status", {}).get("message", {})
            hidden_review_id = status_message.get("messageId") if state in {
                "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"} else None
            for message in public_task.get("history", []):
                if message.get("role") == "ROLE_AGENT" and (
                    hidden_review_id is None or message.get("messageId") != hidden_review_id
                ):
                    publish(message, "message:" + message.get("messageId", ""))
            if state not in {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}:
                if status_message.get("role") == "ROLE_AGENT":
                    publish(status_message, "message:" + status_message.get("messageId", ""))
            for artifact in public_task.get("artifacts", []):
                publish(artifact, "artifact:" + artifact.get("artifactId", ""))
        else:
            public_message = MessageToDict(message)
            publish(public_message, "message:" + public_message.get("messageId", ""))
        return RemoteEvent(kind, state, _parts_text(normalized), final, normalized, task_id, context_id,
                           tuple(messages), truncated, True)
    except (ParseError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError, CoreError):
        raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR") from None


def connect_peer(peer, *, headers=None, timeout=30, max_retries=2, retry_delay=0.2):
    """Discover only the registered destination; credentials live in this call only."""
    if not isinstance(peer, Mapping) or peer.get("enabled") is not True:
        raise CoreError("REMOTE_AGENT_DENIED")
    base = peer.get("url")
    registered = _trusted_endpoint(base)
    timeout = _request_timeout(timeout)
    private_headers = _trusted_headers(headers)
    if type(max_retries) is not int or not 0 <= max_retries <= 10:
        raise CoreError("CONFIG_INVALID")
    if isinstance(retry_delay, bool) or not isinstance(retry_delay, (int, float)) or not math.isfinite(retry_delay) or not 0 <= retry_delay <= 60:
        raise CoreError("CONFIG_INVALID")
    request = Request(base.rstrip("/") + AGENT_CARD_WELL_KNOWN_PATH,
                      headers={"Accept": "application/json", "A2A-Version": A2A_PROTOCOL_VERSION, **private_headers}, method="GET")
    for attempt in range(max_retries + 1):
        try:
            with _open(request, timeout, retryable_status_codes=(500, 502, 503, 504)) as response:
                body = _read_body(response)
            break
        except CoreError as error:
            if not error.retryable or attempt == max_retries:
                raise CoreError(error.code, retryable=error.retryable) from None
            time.sleep(retry_delay)
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError()
        interfaces = payload.get("supportedInterfaces")
        if not isinstance(interfaces, list):
            raise ValueError()
        for interface in interfaces:
            if (not isinstance(interface, dict) or interface.get("protocolBinding") not in {"JSONRPC", "HTTP+JSON"}
                    or interface.get("protocolVersion") != A2A_PROTOCOL_VERSION):
                continue
            target = _trusted_endpoint(interface.get("url"))
            def identity(url):
                return (url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80), url.path.rstrip("/"))
            if identity(target) != identity(registered):
                continue
            capabilities = payload.get("capabilities", {})
            skills = payload.get("skills", [])
            if not isinstance(capabilities, dict) or not isinstance(skills, list) or any(not isinstance(skill, dict) for skill in skills):
                raise ValueError()
            return RemoteAgentConnection(RemoteAgentCard(
                name=_trusted_text(peer.get("name")), description=peer.get("description", ""),
                url=base, streaming=capabilities.get("streaming") is True,
                skills=tuple(skills), binding=interface["protocolBinding"],
            ), timeout=timeout)
    except (ValueError, TypeError, RecursionError, CoreError):
        raise CoreError("REMOTE_AGENT_CARD_INVALID") from None
    raise CoreError("REMOTE_AGENT_CARD_INVALID")


def _require_text(value, code, detail):
    if not isinstance(value, str) or not value.strip():
        raise CoreError(code, detail)
    return value


def _optional_text(value, code, detail):
    if value is None:
        return None
    return _require_text(value, code, detail)


def _number(value, detail, *, minimum, integer=False, exclusive=False):
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError) as error:
        raise CoreError("CONFIG_INVALID", detail) from error
    # Negated comparison also rejects NaN, which compares false against any bound.
    if not (number > minimum if exclusive else number >= minimum):
        raise CoreError("CONFIG_INVALID", detail)
    return number


def _sequence(values, detail):
    if isinstance(values, str | bytes | Mapping):
        raise CoreError("CONFIG_INVALID", detail)
    try:
        return tuple(values)
    except TypeError as error:
        raise CoreError("CONFIG_INVALID", detail) from error


def _validate_endpoint(url):
    _require_text(url, "CONFIG_INVALID", "agent url is required")
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError as error:
        raise CoreError("REMOTE_AGENT_DENIED", "malformed agent url") from error
    if parsed.username or parsed.password:
        raise CoreError("REMOTE_AGENT_DENIED", "credentials in url are not allowed")
    loopback = hostname == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(hostname or "").is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise CoreError("REMOTE_AGENT_DENIED", "insecure agent url")
    return parsed


def build_forwarded_headers(incoming, *, api_key=None):
    if not isinstance(incoming, Mapping):
        raise CoreError("CONFIG_INVALID", "forwarded headers must be a mapping")
    lowered = {
        key.lower(): value
        for key, value in incoming.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    headers = {}
    for canonical in FORWARDED_CLIENT_HEADERS:
        value = (lowered.get(canonical.lower()) or "").strip()
        if not value:
            continue
        if any(char in value for char in "\r\n\x00"):
            raise CoreError("REMOTE_AGENT_DENIED", "invalid forwarded header value")
        headers[canonical] = value
    if api_key is not None:
        key = _require_text(api_key, "CONFIG_INVALID", "api key must be text").strip()
        if any(char in key for char in "\r\n\x00"):
            raise CoreError("CONFIG_INVALID", "api key must not contain control bytes")
        headers["Authorization"] = f"Api-Key {key}"
    return headers


def _safe_error_message(error):
    message = error.get("message") if isinstance(error, Mapping) else None
    if not isinstance(message, str) or not message.strip():
        return "remote agent returned an error"
    return redact(message.strip())[:MAX_ERROR_MESSAGE_CHARS]


def _parts(container):
    parts = container.get("parts") if isinstance(container, Mapping) else None
    if not isinstance(parts, list):
        return ()
    return tuple(part for part in parts if isinstance(part, Mapping))


def _parts_text(parts):
    return "\n".join(part["text"] for part in parts if isinstance(part.get("text"), str))


def _task_parts(task):
    status = task.get("status")
    message_parts = _parts(status.get("message")) if isinstance(status, Mapping) else ()
    if message_parts:
        return message_parts
    artifacts = task.get("artifacts")
    if not isinstance(artifacts, list):
        return ()
    collected = []
    for artifact in artifacts:
        collected.extend(_parts(artifact))
    return tuple(collected)


def _state(container):
    status = container.get("status")
    state = status.get("state") if isinstance(status, Mapping) else None
    return state if isinstance(state, str) else None


def _normalize(result):
    """Read one A2A 1.0 SendMessageResponse or StreamResponse payload."""
    if not isinstance(result, Mapping):
        raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR", "unexpected result payload")
    if isinstance(result.get("statusUpdate"), Mapping):
        update = result["statusUpdate"]
        status = update.get("status")
        parts = _parts(status.get("message")) if isinstance(status, Mapping) else ()
        state = _state(update)
        return RemoteEvent(
            "status", state, _parts_text(parts), state in TERMINAL_TASK_STATES, parts
        )
    if isinstance(result.get("artifactUpdate"), Mapping):
        parts = _parts(result["artifactUpdate"].get("artifact"))
        # Only a status update carries stream finality; lastChunk ends one artifact.
        return RemoteEvent("artifact", None, _parts_text(parts), False, parts)
    if isinstance(result.get("task"), Mapping):
        parts = _task_parts(result["task"])
        state = _state(result["task"])
        return RemoteEvent(
            "task", state, _parts_text(parts), state in TERMINAL_TASK_STATES, parts
        )
    if isinstance(result.get("message"), Mapping):
        parts = _parts(result["message"])
        return RemoteEvent("message", None, _parts_text(parts), True, parts)
    raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR", "unrecognized event kind")


def _interface_url(interfaces):
    """Pick the JSON-RPC 1.x endpoint; a peer without one cannot be called."""
    major = A2A_PROTOCOL_VERSION.split(".")[0]
    for item in interfaces if isinstance(interfaces, list) else ():
        if (
            isinstance(item, Mapping)
            and item.get("protocolBinding") == A2A_BINDING
            and str(item.get("protocolVersion", "")).split(".")[0] == major
            and isinstance(item.get("url"), str)
            and item["url"].strip()
        ):
            return item["url"]
    raise CoreError(
        "REMOTE_AGENT_CARD_INVALID",
        f"card declares no {A2A_BINDING} {A2A_PROTOCOL_VERSION} interface",
    )


def _is_sentinel(payload):
    """Empty events and the ``[DONE]`` sentinel carry no JSON-RPC envelope."""
    stripped = payload.strip()
    return not stripped or stripped == STREAM_DONE_SENTINEL


def _decode_rpc(payload):
    try:
        message = json.loads(payload)
    except ValueError as error:
        raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR", "malformed json") from error
    if not isinstance(message, Mapping):
        raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR", "malformed json-rpc envelope")
    if message.get("error") is not None:
        raise CoreError("REMOTE_AGENT_FAILED", _safe_error_message(message["error"]))
    return _normalize(message.get("result"))


def _open(request, timeout, *, retryable_status_codes=()):
    try:
        return _OPENER.open(request, timeout=timeout)
    except HTTPError as error:
        error.close()
        retryable = error.code in retryable_status_codes or (
            error.code in RETRYABLE_CLIENT_STATUS_CODES
        )
        raise CoreError(
            "REMOTE_AGENT_UNAVAILABLE",
            f"remote agent responded with status {error.code}",
            retryable=retryable,
        ) from None
    except ValueError:
        # A malformed redirect can raise after the peer has received a mutation.
        raise CoreError("REMOTE_AGENT_DENIED") from None
    except (URLError, HTTPException, TimeoutError, OSError) as error:
        raise CoreError(
            "REMOTE_AGENT_UNAVAILABLE", "remote agent is unreachable", retryable=True
        ) from error


def _read_body(response, limit=None):
    limit = MAX_RESPONSE_BYTES if limit is None else limit
    try:
        body = response.read(limit + 1)
    except (URLError, HTTPException, TimeoutError, OSError) as error:
        raise CoreError(
            "REMOTE_AGENT_UNAVAILABLE", "remote agent response failed", retryable=True
        ) from error
    if len(body) > limit:
        raise CoreError("REMOTE_AGENT_RESPONSE_TOO_LARGE")
    return body


class RemoteAgentConnection:
    """Synchronous A2A 1.0 JSON-RPC client for one downstream agent."""

    def __init__(self, card, *, timeout=600.0, api_key=None):
        if not isinstance(card, RemoteAgentCard):
            raise CoreError("CONFIG_INVALID", "card must be a RemoteAgentCard")
        timeout = _number(
            timeout, "timeout must be positive", minimum=0, exclusive=True
        )
        _validate_endpoint(card.url)
        if api_key is not None:
            _require_text(api_key, "CONFIG_INVALID", "api key must be text")
        self.card = card
        self.timeout = timeout
        self.api_key = api_key

    @property
    def supports_streaming(self) -> bool:
        return bool(self.card.streaming)

    def _task_request(self, method, params, *, headers, timeout, request_id=None, attachment_limit_bytes=None):
        try:
            _attachment_limit(attachment_limit_bytes)
            body_limit = MAX_RESPONSE_BYTES if attachment_limit_bytes is None else MAX_FILE_RESPONSE_BYTES
            _trusted_endpoint(self.card.url)
            timeout = _request_timeout(self.timeout if timeout is None else timeout)
            private_headers = _trusted_headers(headers)
            request_id = request_id or str(uuid.uuid4())
            url = self.card.url
            verb = "POST"
            if self.card.binding == "JSONRPC":
                payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            elif self.card.binding == "HTTP+JSON":
                url = url.rstrip("/")
                payload = params
                if method == "SendMessage":
                    url += "/message:send"
                else:
                    identifier = quote(params["id"], safe="")
                    if identifier in {".", ".."}:
                        identifier = identifier.replace(".", "%2E")
                    url += "/tasks/" + identifier
                    if method == "GetTask":
                        verb, payload = "GET", None
                    else:
                        url += ":cancel"
            else:
                raise CoreError("REMOTE_AGENT_CARD_INVALID")
            body = json.dumps(payload, allow_nan=False).encode("utf-8") if payload is not None else None
            if body is not None and len(body) > body_limit:
                raise CoreError("REMOTE_AGENT_REQUEST_TOO_LARGE")
            request = Request(url, data=body,
                              headers={"Content-Type": "application/json", "Accept": "application/json",
                                       "A2A-Version": A2A_PROTOCOL_VERSION, **private_headers}, method=verb)
        except CoreError as error:
            if method == "SendMessage":
                raise ExecutionNotStarted(error.code, data=error.data) from None
            raise
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
            error_type = ExecutionNotStarted if method == "SendMessage" else CoreError
            raise error_type("INVALID_REQUEST") from None
        try:
            with _open(request, timeout, retryable_status_codes=(500, 502, 503, 504) if method == "GetTask" else ()) as response:
                body = _read_body(response, body_limit)
        except CoreError as error:
            # Mutation retryability belongs to the durable intent owner, never this adapter.
            raise CoreError(error.code, retryable=error.retryable and method == "GetTask") from None
        try:
            result = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            _check_depth(result)
            if self.card.binding == "JSONRPC":
                if not isinstance(result, dict) or result.get("jsonrpc") != "2.0" or result.get("id") != request_id:
                    raise ValueError()
                if "error" in result:
                    raise CoreError("REMOTE_AGENT_FAILED")
                result = result["result"]
        except (ValueError, TypeError, KeyError, RecursionError):
            raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR") from None
        return _task_event(result, direct=method != "SendMessage", expected_task_id=params.get("id"),
                           attachment_limit_bytes=attachment_limit_bytes)

    def send_task(self, *, task, message_id, headers=None, timeout=None, files=(), attachment_limit_bytes=None):
        """Send one new task nonblocking; no caller task/context or inherited auth."""
        try:
            _require_text(task, "INVALID_REQUEST", "task must be non-empty text")
            task.encode("utf-8")
            _trusted_text(message_id, code="INVALID_REQUEST")
            parts = [{"text": task}, *_outgoing_parts(files, _attachment_limit(attachment_limit_bytes))]
        except CoreError as error:
            raise ExecutionNotStarted(error.code, data=error.data) from None
        except UnicodeError:
            raise ExecutionNotStarted("INVALID_REQUEST") from None
        return self._task_request("SendMessage", {
            "message": {"role": "ROLE_USER", "messageId": message_id, "parts": parts},
            "configuration": {"returnImmediately": True},
        }, headers=headers, timeout=timeout, request_id=message_id, attachment_limit_bytes=attachment_limit_bytes)

    def get_task(self, *, task_id, headers=None, timeout=None, attachment_limit_bytes=None):
        _trusted_text(task_id, code="INVALID_REQUEST")
        return self._task_request("GetTask", {"id": task_id}, headers=headers, timeout=timeout, attachment_limit_bytes=attachment_limit_bytes)

    def cancel_task(self, *, task_id, headers=None, timeout=None, attachment_limit_bytes=None):
        _trusted_text(task_id, code="INVALID_REQUEST")
        return self._task_request("CancelTask", {"id": task_id}, headers=headers, timeout=timeout, attachment_limit_bytes=attachment_limit_bytes)

    def _request(
        self,
        method,
        *,
        accept,
        task,
        message_id,
        task_id,
        context_id,
        forwarded_headers,
    ):
        _require_text(task, "INVALID_REQUEST", "task must be non-empty text")
        _require_text(
            message_id, "INVALID_REQUEST", "message_id must be non-empty text"
        )
        _optional_text(task_id, "INVALID_REQUEST", "task_id must be non-empty text")
        _optional_text(
            context_id, "INVALID_REQUEST", "context_id must be non-empty text"
        )
        message = {
            "role": "ROLE_USER",
            "messageId": message_id,
            "parts": [{"text": task}],
        }
        if task_id:
            message["taskId"] = task_id
        if context_id:
            message["contextId"] = context_id
        payload = {
            "jsonrpc": "2.0",
            "id": message_id,
            "method": method,
            "params": {"message": message},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": accept,
            "A2A-Version": A2A_PROTOCOL_VERSION,
        }
        headers.update(
            build_forwarded_headers(forwarded_headers or {}, api_key=self.api_key)
        )
        return Request(
            self.card.url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )

    def stream_message(
        self,
        *,
        task,
        message_id,
        task_id=None,
        context_id=None,
        forwarded_headers=None,
    ) -> Iterator[RemoteEvent]:
        request = self._request(
            "SendStreamingMessage",
            accept="text/event-stream",
            task=task,
            message_id=message_id,
            task_id=task_id,
            context_id=context_id,
            forwarded_headers=forwarded_headers,
        )
        return self._iterate(request)

    def _iterate(self, request):
        response = _open(request, self.timeout)
        frames = 0
        received = 0
        data = []
        with response:
            while True:
                try:
                    raw = response.readline(MAX_RESPONSE_BYTES + 1)
                except (URLError, HTTPException, TimeoutError, OSError) as error:
                    raise CoreError(
                        "REMOTE_AGENT_UNAVAILABLE",
                        "remote agent stream failed",
                        retryable=True,
                    ) from error
                if not raw:
                    break
                received += len(raw)
                if received > MAX_RESPONSE_BYTES:
                    raise CoreError("REMOTE_AGENT_RESPONSE_TOO_LARGE")
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line:
                    # SSE strips one optional space and joins the data fields
                    # of one event with a newline; other fields are ignored.
                    if line.startswith("data:"):
                        data.append(line[len("data:") :].removeprefix(" "))
                    continue
                payload = "\n".join(data)
                data = []
                if _is_sentinel(payload):
                    continue
                frames += 1
                if frames > MAX_STREAM_FRAMES:
                    raise CoreError("REMOTE_AGENT_STREAM_TOO_LONG")
                yield _decode_rpc(payload)
            # A stream may end without the blank line that terminates the event.
            payload = "\n".join(data)
            if not _is_sentinel(payload):
                yield _decode_rpc(payload)

    def send_message(
        self,
        *,
        task,
        message_id,
        task_id=None,
        context_id=None,
        forwarded_headers=None,
    ) -> RemoteEvent:
        request = self._request(
            "SendMessage",
            accept="application/json",
            task=task,
            message_id=message_id,
            task_id=task_id,
            context_id=context_id,
            forwarded_headers=forwarded_headers,
        )
        response = _open(request, self.timeout)
        with response:
            body = _read_body(response)
        return replace(_decode_rpc(body.decode("utf-8", "replace")), final=True)


class RemoteAgentRegistry:
    """Loads agent cards once at startup and skips permanently unreachable peers."""

    def __init__(
        self,
        urls,
        *,
        timeout,
        max_retries,
        retry_delay,
        retry_backoff,
        retryable_status_codes,
        api_key=None,
    ):
        self.urls = tuple(
            _require_text(url, "CONFIG_INVALID", "agent url must be text").strip()
            for url in _sequence(urls, "agent urls must be a sequence")
        )
        self.timeout = _number(
            timeout, "timeout must be positive", minimum=0, exclusive=True
        )
        self.max_retries = _number(
            max_retries, "max retries must not be negative", minimum=0, integer=True
        )
        self.retry_delay = _number(
            retry_delay, "retry delay must not be negative", minimum=0
        )
        self.retry_backoff = _number(
            retry_backoff, "retry backoff must be at least 1", minimum=1
        )
        codes = _sequence(
            retryable_status_codes, "retryable status codes must be a sequence"
        )
        self.retryable_status_codes = tuple(
            _number(code, "status code must be an int", minimum=0, integer=True)
            for code in codes
        )
        if api_key is not None:
            _require_text(api_key, "CONFIG_INVALID", "api key must be text")
        self.api_key = api_key
        self._connections = {}
        self._failures = ()


    @property
    def failures(self) -> tuple[tuple[str, str], ...]:
        return self._failures

    def connect(self) -> dict[str, RemoteAgentConnection]:
        connections = {}
        failures = []
        for index, url in enumerate(self.urls):
            base_url = url.rstrip("/")
            try:
                card = self._card(base_url, index)
                connection = RemoteAgentConnection(
                    card, timeout=self.timeout, api_key=self.api_key
                )
            except CoreError as error:
                failures.append((base_url, error.code))
                continue
            if card.name in connections:
                failures.append((base_url, "REMOTE_AGENT_DUPLICATE_NAME"))
                continue
            connections[card.name] = connection
        self._connections = connections
        self._failures = tuple(failures)
        return dict(connections)

    def _fallback_name(self, base_url, index):
        segments = [item for item in urlparse(base_url).path.split("/") if item]
        if "a2a" in segments:
            position = segments.index("a2a") + 1
            if position < len(segments):
                return segments[position]
        return f"remote_agent_{index + 1}"

    def _card(self, base_url, index):
        payload = self._fetch_card(base_url)
        name = payload.get("name")
        description = payload.get("description")
        capabilities = payload.get("capabilities")
        skills = payload.get("skills")
        return RemoteAgentCard(
            name=name.strip()
            if isinstance(name, str) and name.strip()
            else self._fallback_name(base_url, index),
            description=description if isinstance(description, str) else "",
            url=_interface_url(payload.get("supportedInterfaces")),
            streaming=bool(
                capabilities.get("streaming")
                if isinstance(capabilities, Mapping)
                else False
            ),
            skills=tuple(
                dict(skill)
                for skill in (skills if isinstance(skills, list) else ())
                if isinstance(skill, Mapping)
            ),
        )

    def _fetch_card(self, base_url):
        endpoint = base_url + AGENT_CARD_WELL_KNOWN_PATH
        _validate_endpoint(endpoint)
        headers = {"Accept": "application/json"}
        headers.update(build_forwarded_headers({}, api_key=self.api_key))
        attempt = 0
        while True:
            request = Request(endpoint, headers=headers, method="GET")
            try:
                response = _open(
                    request,
                    self.timeout,
                    retryable_status_codes=self.retryable_status_codes,
                )
                with response:
                    body = _read_body(response)
            except CoreError as error:
                if not error.retryable or attempt >= self.max_retries:
                    raise
                time.sleep(self.retry_delay * self.retry_backoff**attempt)
                attempt += 1
                continue
            try:
                payload = json.loads(body)
            except ValueError as error:
                raise CoreError(
                    "REMOTE_AGENT_CARD_INVALID", "malformed json"
                ) from error
            if not isinstance(payload, Mapping):
                raise CoreError("REMOTE_AGENT_CARD_INVALID", "card must be an object")
            return payload
