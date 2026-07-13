from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .errors import CoreError


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
    ):
        if api_format not in {"openai", "anthropic"} or not model:
            raise CoreError("CONFIG_INVALID")
        suffix = "/chat/completions" if api_format == "openai" else "/messages"
        default = (
            "https://api.openai.com/v1"
            if api_format == "openai"
            else "https://api.anthropic.com/v1"
        )
        self.endpoint = endpoint or self._endpoint(base_url or default, suffix)
        if urlparse(self.endpoint).scheme not in {"http", "https"}:
            raise CoreError("CONFIG_INVALID")
        self.api_format = api_format
        self.model = model
        hostname = urlparse(self.endpoint).hostname or ""
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
        if self.context_window <= self.max_tokens or self.token_chars <= 0:
            raise CoreError("CONFIG_INVALID")

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
    def _wire_name(name):
        if len(name) <= 64 and re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            return name
        stem = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:54]
        return f"{stem}_{hashlib.sha256(name.encode()).hexdigest()[:8]}"

    def _tools(self, catalog):
        definitions = (
            catalog if isinstance(catalog, dict) else {name: {} for name in catalog}
        )
        reverse = {self._wire_name(name): name for name in sorted(definitions)}
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
                result.append(
                    {
                        "role": "assistant",
                        "content": message.get("content"),
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

    def _request(self, context, instructions, tools, messages=None):
        schemas, reverse = self._tools(tools)
        messages = list(messages or ({"role": "user", "content": context},))
        body = dict(self.extra_body)
        if self.api_format == "openai":
            body.update(
                {
                    "model": self.model,
                    "messages": [{"role": "system", "content": instructions}]
                    + self._openai_messages(messages, reverse),
                }
            )
        else:
            body.update(
                {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "system": instructions,
                    "messages": self._anthropic_messages(messages, reverse),
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

    def _post(self, body, headers):
        request = Request(
            self.endpoint,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(16_777_217)
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
        return value

    @staticmethod
    def _public_text(value):
        value = re.sub(r"(?is)<think>.*?</think>\s*", "", value)
        value = re.sub(r"(?is)<think>.*$", "", value).strip()
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
        usage = response.get("usage") or {}
        usage_fields = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "finish_reason": response["choices"][0].get("finish_reason"),
        }
        if calls:
            return ModelResponse(tool_requests=tuple(calls), **usage_fields)
        if not isinstance(content, str):
            raise CoreError("MODEL_UNAVAILABLE", "model returned no text")
        return ModelResponse(message=self._public_text(content), **usage_fields)

    def _parse_anthropic(self, response, reverse):
        content = response.get("content")
        if not isinstance(content, list):
            raise CoreError("MODEL_UNAVAILABLE", "invalid Anthropic message")
        calls = []
        text = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text.append(str(block.get("text", "")))
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
            return ModelResponse(tool_requests=tuple(calls), **usage_fields)
        return ModelResponse(message=self._public_text("".join(text)), **usage_fields)

    def generate(self, *, context, tools, instructions, messages=None):
        body, headers, reverse = self._request(
            context, instructions, tools, messages=messages
        )
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
