from __future__ import annotations

import ipaddress
import json
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import CoreError
from .security import redact

AGENT_CARD_WELL_KNOWN_PATH = "/.well-known/agent-card.json"
A2A_PROTOCOL_VERSION = "1.0"
A2A_BINDING = "JSONRPC"
FORWARDED_CLIENT_HEADERS = ("Authorization", "X-PROJECT-ID", "X-A2A-Extensions")

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
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


@dataclass(frozen=True)
class RemoteEvent:
    kind: str
    state: str | None
    text: str
    final: bool
    parts: tuple[dict, ...]


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
    except ValueError as error:
        # urllib rejects an unsupported or malformed target before any I/O.
        raise CoreError("REMOTE_AGENT_DENIED", "malformed agent url") from error
    except (URLError, HTTPException, TimeoutError, OSError) as error:
        raise CoreError(
            "REMOTE_AGENT_UNAVAILABLE", "remote agent is unreachable", retryable=True
        ) from error


def _read_body(response):
    try:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    except (URLError, HTTPException, TimeoutError, OSError) as error:
        raise CoreError(
            "REMOTE_AGENT_UNAVAILABLE", "remote agent response failed", retryable=True
        ) from error
    if len(body) > MAX_RESPONSE_BYTES:
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
