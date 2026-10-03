from __future__ import annotations

from collections import Counter
import contextlib
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import CoreError
from .security import validate_http_url
from .streaming import integrate_stream_chunk


@dataclass(frozen=True)
class ToolRequest:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ModelResponse:
    message: str | None = None
    tool_requests: tuple[ToolRequest, ...] = ()
    continue_reasoning: bool = False
    reasoning: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    finish_reason: str | None = None
    reasoning_tokens: int | None = None
    reasoning_replay: dict | None = None


@dataclass(frozen=True)
class ModelCall:
    context: str
    tools: frozenset[str]
    instructions: str
    messages: tuple[dict, ...] = ()


class ScriptedModel:
    def __init__(self, responses):
        self._responses = list(responses)
        self._calls = []

    @property
    def calls(self):
        return tuple(self._calls)

    def generate(self, *, context, tools, instructions, messages=None):
        self._calls.append(
            ModelCall(context, frozenset(tools), instructions, tuple(messages or ()))
        )
        if not self._responses:
            raise CoreError("MODEL_UNAVAILABLE")
        return self._responses.pop(0)


class CompatibleHttpModel:
    """Small synchronous adapter for OpenAI-compatible and Anthropic Messages APIs."""

    REASONING_EFFORTS = frozenset(
        {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
    )

    SAMPLING_RANGES = {
        "temperature": (0.0, 2.0),
        "top_p": (0.0, 1.0),
        "frequency_penalty": (-2.0, 2.0),
        "presence_penalty": (-2.0, 2.0),
    }
    ANTHROPIC_UNSUPPORTED_SAMPLING = ("frequency_penalty", "presence_penalty")

    def __init__(
        self,
        *,
        api_format,
        model,
        provider=None,
        base_url=None,
        endpoint=None,
        api_key=None,
        timeout=120,
        max_tokens=4096,
        headers=None,
        extra_body=None,
        anthropic_version="2023-06-01",
        context_window=128_000,
        token_chars=3,
        reasoning_effort=None,
        temperature=None,
        top_p=None,
        top_k=None,
        frequency_penalty=None,
        presence_penalty=None,
        stream=False,
        cache_ttl=None,
        cache_min_tokens=0,
    ):
        if api_format not in {"openai", "anthropic"}:
            raise CoreError(
                "CONFIG_INVALID", "LLM_API_FORMAT must be openai or anthropic"
            )
        if not model:
            raise CoreError("CONFIG_INVALID", "LLM_MODEL is required")
        suffix = "/chat/completions" if api_format == "openai" else "/messages"
        default = (
            "https://api.openai.com/v1"
            if api_format == "openai"
            else "https://api.anthropic.com/v1"
        )
        self.endpoint = endpoint or self._endpoint(base_url or default, suffix)
        parsed = validate_http_url(self.endpoint, "LLM_ENDPOINT" if endpoint else "LLM_API_BASE")
        self.api_format = api_format
        self.model = model
        hostname = parsed.hostname
        inferred_provider = next(
            (
                candidate
                for candidate in ("openai", "anthropic", "minimax")
                if candidate in hostname.lower()
            ),
            api_format,
        )
        self.provider = provider or inferred_provider
        self.api_key = api_key
        self.timeout = float(timeout)
        self.max_tokens = int(max_tokens)
        self.headers = dict(headers or {})
        self.extra_body = dict(extra_body or {})
        self.anthropic_version = anthropic_version
        self.context_window = int(context_window)
        self.token_chars = int(token_chars)
        self.reasoning_effort = (
            reasoning_effort.strip().lower()
            if isinstance(reasoning_effort, str) and reasoning_effort.strip()
            else None
        )
        if self.context_window <= self.max_tokens or self.token_chars <= 0:
            raise CoreError("CONFIG_INVALID")
        if (
            reasoning_effort is not None
            and self.reasoning_effort not in self.REASONING_EFFORTS
        ):
            raise CoreError("CONFIG_INVALID", "invalid THINKING_LEVEL")
        self.sampling = self._sampling(
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
        )
        self.stream = bool(stream)
        self.cache_ttl = cache_ttl
        self.cache_min_tokens = max(0, int(cache_min_tokens))
        self.invocation_parameters

    def _sampling(self, **values):
        sampling = {}
        for name, value in values.items():
            if value is None:
                continue
            if name == "top_k":
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise CoreError("CONFIG_INVALID", "LLM_TOP_K must be a positive int")
            else:
                low, high = self.SAMPLING_RANGES[name]
                if not low <= float(value) <= high:
                    raise CoreError(
                        "CONFIG_INVALID", f"LLM_{name.upper()} must be in [{low}, {high}]"
                    )
            if (
                self.api_format == "anthropic"
                and name in self.ANTHROPIC_UNSUPPORTED_SAMPLING
            ):
                raise CoreError(
                    "CONFIG_INVALID", f"Anthropic does not support LLM_{name.upper()}"
                )
            sampling[name] = value
        return sampling

    @property
    def invocation_parameters(self):
        parameters = dict(self.extra_body)
        parameters.update(self.sampling)
        if self.api_format == "openai":
            if self.provider.lower() == "minimax":
                parameters.setdefault("reasoning_split", True)
                if self.reasoning_effort:
                    parameters.setdefault("thinking", {"type": "adaptive"})
            if self.reasoning_effort:
                parameters["reasoning_effort"] = self.reasoning_effort
        elif self.reasoning_effort:
            output_config = parameters.get("output_config", {})
            if not isinstance(output_config, dict):
                raise CoreError("CONFIG_INVALID", "output_config must be an object")
            parameters["output_config"] = {
                **output_config,
                "effort": self.reasoning_effort,
            }
            parameters.setdefault("thinking", {"type": "adaptive"})
        return parameters

    def count_tokens(self, text):
        """Conservative provider-neutral estimate when no tokenizer endpoint exists."""
        return max(
            1, (len(text.encode("utf-8")) + self.token_chars - 1) // self.token_chars
        )

    @staticmethod
    def _endpoint(base_url, suffix):
        base = base_url.rstrip("/")
        return base if base.endswith(suffix) else base + suffix

    @staticmethod
    def _wire_name(name, *, disambiguate=False):
        if len(name) <= 64 and re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            return name
        stem = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
        if len(stem) <= 64 and not disambiguate:
            return stem
        stem = stem[:55]
        return f"{stem}_{hashlib.sha256(name.encode()).hexdigest()[:8]}"

    def _tools(self, catalog):
        definitions = (
            catalog if isinstance(catalog, dict) else {name: {} for name in catalog}
        )
        candidates = {name: self._wire_name(name) for name in sorted(definitions)}
        counts = Counter(candidates.values())
        collisions = {
            wire_name
            for wire_name in candidates.values()
            if counts[wire_name] > 1
        }
        reverse = {
            self._wire_name(name, disambiguate=wire_name in collisions): name
            for name, wire_name in candidates.items()
        }
        if len(reverse) != len(definitions):
            raise CoreError("TOOL_NAME_COLLISION")
        schemas = []
        for wire_name in reverse:
            definition = definitions[reverse[wire_name]]
            schema = definition.get("input_schema") or {
                "type": "object",
                "additionalProperties": True,
            }
            description = definition.get("description") or reverse[wire_name]
            if wire_name != reverse[wire_name]:
                description = (
                    f"Canonical tool name: {reverse[wire_name]}. {description}"
                )
            if self.api_format == "openai":
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": wire_name,
                            "description": description,
                            "parameters": schema,
                        },
                    }
                )
            else:
                schemas.append(
                    {
                        "name": wire_name,
                        "description": description,
                        "input_schema": schema,
                    }
                )
        return schemas, reverse

    @staticmethod
    def _openai_messages(messages, reverse):
        logical_to_wire = {logical: wire for wire, logical in reverse.items()}
        result = []
        for message in messages:
            if message["role"] == "assistant" and message.get("tool_calls"):
                replay = message.get("reasoning_replay") or {}
                fields = (
                    replay.get("fields", {})
                    if replay.get("format") == "openai"
                    else {}
                )
                result.append(
                    {
                        "role": "assistant",
                        **fields,
                        "content": fields.get("content", message.get("content")),
                        "tool_calls": [
                            {
                                "id": call["id"],
                                "type": "function",
                                "function": {
                                    "name": logical_to_wire[call["function"]["name"]],
                                    "arguments": json.dumps(
                                        call["function"]["arguments"],
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    ),
                                },
                            }
                            for call in message["tool_calls"]
                        ],
                    }
                )
                continue
            current = dict(message)
            if current["role"] == "tool" and current.get("name"):
                current["name"] = logical_to_wire[current["name"]]
            result.append(current)
        return result

    @staticmethod
    def _anthropic_messages(messages, reverse):
        logical_to_wire = {logical: wire for wire, logical in reverse.items()}
        result = []
        for message in messages:
            if message["role"] == "assistant" and message.get("tool_calls"):
                replay = message.get("reasoning_replay") or {}
                if replay.get("format") == "anthropic" and isinstance(
                    replay.get("content"), list
                ):
                    result.append(
                        {"role": "assistant", "content": replay["content"]}
                    )
                    continue
                result.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": call["id"],
                                "name": logical_to_wire[call["function"]["name"]],
                                "input": call["function"]["arguments"],
                            }
                            for call in message["tool_calls"]
                        ],
                    }
                )
                continue
            if message["role"] == "tool":
                result.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message["tool_call_id"],
                                "content": message["content"],
                            }
                        ],
                    }
                )
                continue
            result.append({"role": message["role"], "content": message["content"]})
        return result

    def _system_blocks(self, instructions):
        """Mark the stable instruction prefix cacheable (Anthropic prompt caching).

        OpenAI-compatible providers cache the prefix automatically, so the same
        settings need no request field there.
        """
        if not self.cache_ttl or self.count_tokens(instructions) < self.cache_min_tokens:
            return instructions
        return [
            {
                "type": "text",
                "text": instructions,
                "cache_control": {"type": "ephemeral", "ttl": self.cache_ttl},
            }
        ]

    def _request(self, context, instructions, tools, messages=None):
        schemas, reverse = self._tools(tools)
        messages = list(messages or ({"role": "user", "content": context},))
        # Historical protocol is replay data, not the current dispatch authority.
        replay_names = dict(reverse)
        historical_names = {call["function"]["name"] for message in messages
                            for call in message.get("tool_calls", ())}
        historical_names.update(message["name"] for message in messages
                                if message.get("role") == "tool" and message.get("name"))
        for name in sorted(historical_names - set(reverse.values())):
            wire_name = self._wire_name(name)
            if wire_name in replay_names:
                wire_name = self._wire_name(name, disambiguate=True)
            if wire_name in replay_names:
                raise CoreError("TOOL_NAME_COLLISION")
            replay_names[wire_name] = name
        body = self.invocation_parameters
        # Provider options cannot reintroduce capabilities hidden by runtime
        # policy, including the detector's deliberately empty catalog.
        for key in ("tools", "tool_choice", "functions", "function_call"):
            body.pop(key, None)
        if self.api_format == "openai":
            body.update(
                {
                    "model": self.model,
                    "messages": [{"role": "system", "content": instructions}]
                    + self._openai_messages(messages, replay_names),
                }
            )
        else:
            body.update(
                {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "system": self._system_blocks(instructions),
                    "messages": self._anthropic_messages(messages, replay_names),
                }
            )
        if schemas:
            body["tools"] = schemas
        headers = {"Content-Type": "application/json", **self.headers}
        if self.api_format == "openai" and self.api_key:
            headers.setdefault("Authorization", f"Bearer {self.api_key}")
        if self.api_format == "anthropic":
            if self.api_key:
                headers.setdefault("x-api-key", self.api_key)
            headers.setdefault("anthropic-version", self.anthropic_version)
        return body, headers, reverse

    def _open(self, body, headers):
        request = Request(
            self.endpoint,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        try:
            return urlopen(request, timeout=self.timeout)
        except HTTPError as error:
            detail = error.read(4096).decode(errors="replace")
            retryable = error.code in {408, 409, 429} or error.code >= 500
            raise CoreError(
                "MODEL_UNAVAILABLE",
                f"model HTTP {error.code}: {detail}",
                retryable=retryable,
            ) from error
        except (OSError, TimeoutError, URLError) as error:
            raise CoreError("MODEL_UNAVAILABLE", str(error), retryable=True) from error

    @contextlib.contextmanager
    def _opened(self, body, headers):
        """Cover the response body, not only the connection.

        The socket timeout applies to every read, so a stalled body — or, on a
        stream, a gap between chunks longer than LLM_TIMEOUT — raises here and
        not at connect time. Converting only the connect leaves that as a bare
        TimeoutError: no stable code, no `retryable`, no reflect-and-retry, and
        an A2A Task that dies with a traceback instead of a failure.
        """
        with self._open(body, headers) as response:
            try:
                yield response
            except CoreError:
                raise
            except (OSError, TimeoutError, URLError, HTTPException) as error:
                raise CoreError(
                    "MODEL_UNAVAILABLE", str(error), retryable=True
                ) from error

    @staticmethod
    def _server_sent_events(stream):
        """Yield decoded `data:` payloads from a text/event-stream response."""
        for raw in stream:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                yield json.loads(payload)
            except json.JSONDecodeError as error:
                raise CoreError(
                    "MODEL_UNAVAILABLE", "invalid model stream frame"
                ) from error

    def _stream_openai(self, stream, publish):
        message = {"content": None, "tool_calls": []}
        reasoning = ""
        response = {"choices": [{"message": message, "finish_reason": None}]}
        for frame in self._server_sent_events(stream):
            if frame.get("usage"):
                response["usage"] = frame["usage"]
            for choice in frame.get("choices") or ():
                if choice.get("finish_reason"):
                    response["choices"][0]["finish_reason"] = choice["finish_reason"]
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    message["content"] = integrate_stream_chunk(
                        message["content"] or "", content
                    )
                # Unstripped, like the content delta beside it: the reasoning
                # arrives token by token, and trimming each one glues the words
                # together in the assembled text.
                visible = self._visible_reasoning(
                    delta.get("reasoning_details"), strip=False
                ) or self._visible_reasoning(
                    delta.get("reasoning_content") or delta.get("reasoning"),
                    strip=False,
                )
                if visible:
                    reasoning = integrate_stream_chunk(reasoning, visible)
                for call in delta.get("tool_calls") or ():
                    self._merge_openai_tool_call(message["tool_calls"], call)
                if content or visible:
                    publish(message["content"] or "", reasoning)
        if reasoning:
            message["reasoning_content"] = reasoning
        if not message["tool_calls"]:
            message.pop("tool_calls")
        for call in message.get("tool_calls", ()):
            # A zero-argument tool may arrive with no argument delta at all.
            call["function"]["arguments"] = call["function"]["arguments"] or "{}"
        return response

    @staticmethod
    def _merge_openai_tool_call(calls, delta):
        index = delta.get("index", len(calls))
        while len(calls) <= index:
            calls.append({"id": None, "function": {"name": "", "arguments": ""}})
        current = calls[index]
        if delta.get("id"):
            current["id"] = delta["id"]
        function = delta.get("function") or {}
        if function.get("name"):
            current["function"]["name"] = (
                current["function"]["name"] or ""
            ) + function["name"]
        if function.get("arguments"):
            current["function"]["arguments"] += function["arguments"]

    def _stream_anthropic(self, stream, publish):
        blocks = []
        response = {"content": blocks, "usage": {}, "stop_reason": None}
        text = ""
        reasoning = ""
        for frame in self._server_sent_events(stream):
            kind = frame.get("type")
            if kind == "message_start":
                response["usage"].update(
                    (frame.get("message") or {}).get("usage") or {}
                )
            elif kind == "content_block_start":
                block = dict(frame.get("content_block") or {})
                block.setdefault("type", "text")
                if block["type"] == "tool_use":
                    block.setdefault("input", {})
                    block["_partial_json"] = ""
                blocks.append(block)
            elif kind == "content_block_delta":
                if not blocks:
                    continue
                block = blocks[-1]
                delta = frame.get("delta") or {}
                if delta.get("type") == "text_delta":
                    chunk = delta.get("text", "")
                    block["text"] = integrate_stream_chunk(block.get("text", ""), chunk)
                    text = integrate_stream_chunk(text, chunk)
                    publish(text, reasoning)
                elif delta.get("type") == "thinking_delta":
                    chunk = delta.get("thinking", "")
                    block["thinking"] = integrate_stream_chunk(
                        block.get("thinking", ""), chunk
                    )
                    reasoning = integrate_stream_chunk(reasoning, chunk)
                    publish(text, reasoning)
                elif delta.get("type") == "signature_delta":
                    block["signature"] = block.get("signature", "") + delta.get(
                        "signature", ""
                    )
                elif delta.get("type") == "input_json_delta":
                    block["_partial_json"] += delta.get("partial_json", "")
            elif kind == "content_block_stop" and blocks:
                block = blocks[-1]
                if block.get("type") == "tool_use":
                    partial = block.pop("_partial_json", "")
                    block["input"] = json.loads(partial) if partial.strip() else {}
            elif kind == "message_delta":
                response["usage"].update(frame.get("usage") or {})
                stop = (frame.get("delta") or {}).get("stop_reason")
                if stop:
                    response["stop_reason"] = stop
        return response

    def _post_stream(self, body, headers, reverse, on_delta):
        def publish(content, reasoning):
            if on_delta is None:
                return
            public, embedded = self._split_reasoning(content, reverse)
            on_delta(public, self._canonical_text(reasoning, reverse) or embedded)

        headers = {**headers, "Accept": "text/event-stream"}
        with self._opened(body, headers) as stream:
            if self.api_format == "openai":
                return self._stream_openai(stream, publish)
            return self._stream_anthropic(stream, publish)

    def _post(self, body, headers):
        with self._opened(body, headers) as response:
            raw = response.read(16_777_217)
        if len(raw) > 16_777_216:
            raise CoreError("MODEL_UNAVAILABLE", "model response is too large")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CoreError(
                "MODEL_UNAVAILABLE", "invalid model JSON response"
            ) from error

    @staticmethod
    def _arguments(value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as error:
                raise CoreError(
                    "MODEL_UNAVAILABLE", "invalid tool arguments"
                ) from error
        if not isinstance(value, dict):
            raise CoreError("MODEL_UNAVAILABLE", "tool arguments must be an object")
        # An explicit null is the model saying it has nothing for that argument,
        # which is what omitting it means. Keeping it would fail validation on a
        # perfectly ordinary call, and it is how a schema that marks optional
        # fields nullable — as strict mode requires — is answered.
        return {key: item for key, item in value.items() if item is not None}

    @staticmethod
    def _canonical_text(value, reverse=None, *, strip=True):
        for wire_name, canonical_name in sorted(
            (reverse or {}).items(), key=lambda item: len(item[0]), reverse=True
        ):
            if wire_name != canonical_name:
                value = value.replace(wire_name, canonical_name)
        # Trimming is right for a whole message and wrong for a fragment of one:
        # the space between two streamed tokens lives at the edge of a chunk.
        return value.strip() if strip else value

    @classmethod
    def _split_reasoning(cls, value, reverse=None):
        reasoning = re.findall(r"(?is)<think>(.*?)</think>", value)
        without_closed = re.sub(r"(?is)<think>.*?</think>", "", value)
        unclosed = re.search(r"(?is)<think>(.*)$", without_closed)
        if unclosed:
            reasoning.append(unclosed.group(1))
        public = re.sub(r"(?is)<think>.*?</think>\s*", "", value)
        public = re.sub(r"(?is)<think>.*$", "", public)
        visible = "\n\n".join(part.strip() for part in reasoning if part.strip())
        return cls._canonical_text(public, reverse), cls._canonical_text(
            visible, reverse
        )

    @classmethod
    def _visible_reasoning(cls, value, reverse=None, *, strip=True):
        if isinstance(value, str):
            return cls._canonical_text(value, reverse, strip=strip)
        if isinstance(value, list):
            parts = [
                cls._visible_reasoning(item, reverse, strip=strip) for item in value
            ]
            return "\n\n".join(part for part in parts if part)
        if isinstance(value, dict):
            for key in ("text", "reasoning", "reasoning_content", "summary"):
                if key in value:
                    visible = cls._visible_reasoning(value[key], reverse, strip=strip)
                    if visible:
                        return visible
        return ""

    @classmethod
    def _public_text(cls, value, reverse=None):
        value, _reasoning = cls._split_reasoning(value, reverse)
        if not value:
            raise CoreError("MODEL_UNAVAILABLE", "model returned no public text")
        return value

    def _parse_openai(self, response, reverse):
        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise CoreError("MODEL_UNAVAILABLE", "invalid chat completion") from error
        calls = []
        for call in message.get("tool_calls") or ():
            function = call.get("function", {})
            try:
                name = reverse[function["name"]]
            except (KeyError, TypeError) as error:
                raise CoreError("MODEL_UNAVAILABLE", "unknown model tool") from error
            calls.append(
                ToolRequest(
                    call.get("id") or str(uuid.uuid4()),
                    name,
                    self._arguments(function.get("arguments", {})),
                )
            )
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        public_content, embedded_reasoning = (
            self._split_reasoning(content, reverse)
            if isinstance(content, str)
            else ("", "")
        )
        reasoning = self._visible_reasoning(
            message.get("reasoning_details"), reverse
        ) or self._visible_reasoning(message.get("reasoning_content"), reverse)
        reasoning = reasoning or embedded_reasoning or None
        usage = response.get("usage") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        usage_fields = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": completion_details.get("reasoning_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "finish_reason": response["choices"][0].get("finish_reason"),
        }
        if calls:
            replay_fields = {
                key: message[key]
                for key in ("content", "reasoning_details", "reasoning_content")
                if key in message
            }
            replay = (
                {"format": "openai", "fields": replay_fields}
                if replay_fields
                and (
                    reasoning
                    or "reasoning_details" in message
                    or "reasoning_content" in message
                )
                else None
            )
            return ModelResponse(
                tool_requests=tuple(calls),
                reasoning=reasoning,
                reasoning_replay=replay,
                **usage_fields,
            )
        if not isinstance(content, str):
            raise CoreError("MODEL_UNAVAILABLE", "model returned no text")
        if not public_content:
            raise CoreError("MODEL_UNAVAILABLE", "model returned no public text")
        return ModelResponse(
            message=public_content, reasoning=reasoning, **usage_fields
        )

    def _parse_anthropic(self, response, reverse):
        content = response.get("content")
        if not isinstance(content, list):
            raise CoreError("MODEL_UNAVAILABLE", "invalid Anthropic message")
        calls = []
        text = []
        reasoning = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text.append(str(block.get("text", "")))
            elif block.get("type") == "thinking":
                visible = self._visible_reasoning(block.get("thinking"), reverse)
                if visible:
                    reasoning.append(visible)
            elif block.get("type") == "tool_use":
                try:
                    name = reverse[block["name"]]
                except KeyError as error:
                    raise CoreError(
                        "MODEL_UNAVAILABLE", "unknown model tool"
                    ) from error
                calls.append(
                    ToolRequest(
                        block.get("id") or str(uuid.uuid4()),
                        name,
                        self._arguments(block.get("input", {})),
                    )
                )
        usage = response.get("usage") or {}
        prompt_tokens = usage.get("input_tokens")
        completion_tokens = usage.get("output_tokens")
        total_tokens = (
            prompt_tokens + completion_tokens
            if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int)
            else None
        )
        usage_fields = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "finish_reason": response.get("stop_reason"),
        }
        if calls:
            return ModelResponse(
                tool_requests=tuple(calls),
                reasoning="\n\n".join(reasoning) or None,
                reasoning_replay={"format": "anthropic", "content": content},
                **usage_fields,
            )
        return ModelResponse(
            message=self._public_text("".join(text), reverse),
            reasoning="\n\n".join(reasoning) or None,
            **usage_fields,
        )

    def generate(self, *, context, tools, instructions, messages=None, on_delta=None):
        body, headers, reverse = self._request(
            context, instructions, tools, messages=messages
        )
        if self.stream:
            body["stream"] = True
            if self.api_format == "openai":
                body.setdefault("stream_options", {"include_usage": True})
            response = self._post_stream(body, headers, reverse, on_delta)
        else:
            response = self._post(body, headers)
        if self.api_format == "openai":
            return self._parse_openai(response, reverse)
        return self._parse_anthropic(response, reverse)


@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int
    tool_use: bool
    structured_output: bool
    modalities: set[str]


@dataclass(frozen=True)
class ModelRoute:
    name: str
    capabilities: ModelCapabilities
    region: str
    data_classes: set[str]
    cost: int


@dataclass(frozen=True)
class FallbackDecision:
    route: ModelRoute
    compaction_required: bool
    do_not_replay_tool_call_ids: frozenset[str]


class ModelRouter:
    def __init__(self, routes):
        self.routes = list(routes)

    def select(
        self, *, required_context, modalities, data_class, allowed_regions, max_cost
    ):
        candidates = [
            route
            for route in self.routes
            if route.capabilities.context_window >= required_context
            and set(modalities) <= set(route.capabilities.modalities)
            and data_class in route.data_classes
            and route.region in allowed_regions
            and route.cost <= max_cost
        ]
        if not candidates:
            raise CoreError("MODEL_UNAVAILABLE")
        return min(candidates, key=lambda route: (route.cost, route.name))

    def fallback(self, *, failed_route, active_context_tokens, completed_tool_call_ids):
        candidates = [route for route in self.routes if route.name != failed_route]
        if not candidates:
            raise CoreError("MODEL_UNAVAILABLE")
        route = min(candidates, key=lambda item: (item.cost, item.name))
        return FallbackDecision(
            route,
            active_context_tokens > route.capabilities.context_window,
            frozenset(completed_tool_call_ids),
        )
