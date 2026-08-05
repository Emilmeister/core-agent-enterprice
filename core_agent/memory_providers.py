"""HTTP adapters for the memory embedding and entity extraction calls.

Both validate the response shape before it reaches the domain and keep the input
text and the credential out of every error they raise.
"""

from __future__ import annotations

import json
import logging
import math
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import CoreError

MAX_PROVIDER_TEXT = 8000
MAX_EXTRACTED_ENTITIES = 1000

ENTITY_TYPES = (
    "person",
    "organization",
    "location",
    "product",
    "technology",
    "event",
    "other",
)

# Sent whole on every request. Naming a schema the gateway is assumed to hold
# would make extraction depend on state this process cannot see or create.
EXTRACTION_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "memory_entity_extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["entities"],
            "properties": {
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["text", "type", "confidence"],
                        "properties": {
                            "text": {"type": "string"},
                            "type": {"enum": list(ENTITY_TYPES)},
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                        },
                    },
                }
            },
        },
    },
}

EXTRACTION_INSTRUCTION = (
    "Extract the named entities from the note. Copy each entity exactly as it "
    "is written in the note, in the note's own language and script; do not "
    "translate, normalise or invent. Return nothing but the entities."
)


def _post_json(
    endpoint, payload, *, headers=None, api_key=None, timeout=30, max_bytes=4_000_000
):
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    if api_key:
        request_headers["Authorization"] = f"Bearer {api_key}"
    try:
        request = Request(
            endpoint,
            data=json.dumps(payload).encode(),
            headers=request_headers,
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            encoded = response.read(max_bytes + 1)
    except HTTPError as error:
        raise CoreError(
            "MEMORY_PROVIDER_UNAVAILABLE",
            "memory provider rejected the request",
            data={"status": error.code},
        ) from None
    except (OSError, TimeoutError, URLError, HTTPException, ValueError):
        # A truncated chunked body raises HTTPException, not OSError, and a
        # malformed base URL raises ValueError from Request itself. Both are
        # provider problems and must not escape as an unhandled exception.
        raise CoreError(
            "MEMORY_PROVIDER_UNAVAILABLE", "memory provider is unreachable"
        ) from None
    if len(encoded) > max_bytes:
        raise CoreError("MEMORY_PROVIDER_INVALID_RESPONSE", "response too large")
    try:
        return json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CoreError(
            "MEMORY_PROVIDER_INVALID_RESPONSE", "response is not JSON"
        ) from None


class HttpEmbeddingProvider:
    """OpenAI-compatible `/embeddings` client.

    Never raises: a failed embedding degrades the vector channel, and a tool call
    that dies because a similarity could not be computed is worse than a search
    answered by BM25 alone.
    """

    def __init__(
        self,
        api_base,
        model,
        *,
        api_key=None,
        dimension=768,
        timeout=30,
        headers=None,
        logger=None,
    ):
        if not api_base or not model:
            raise CoreError(
                "CONFIG_INVALID", "embedding api base and model are both required"
            )
        # Deployments carry the serving-stack prefix in the model name; the API
        # itself does not know it.
        self.model = model.removeprefix("hosted_vllm/")
        self.endpoint = api_base.rstrip("/") + "/embeddings"
        self.api_key = api_key
        self.dimension = int(dimension)
        self.timeout = timeout
        self.headers = dict(headers or {})
        self.logger = logger or logging.getLogger("core_agent.runtime")
        self.version = f"openai-compatible:{self.model}"

    def embed(self, text):
        if not text or not text.strip():
            return None
        try:
            response = _post_json(
                self.endpoint,
                {"model": self.model, "input": text[:MAX_PROVIDER_TEXT]},
                headers=self.headers,
                api_key=self.api_key,
                timeout=self.timeout,
            )
            vector = response["data"][0]["embedding"]
        except CoreError as error:
            self.logger.warning("embedding unavailable (%s)", error.code)
            return None
        except Exception as error:
            # The spec is unconditional: any embedding failure degrades the
            # vector channel. A tool call must never die because a similarity
            # could not be computed.
            self.logger.warning("embedding failed (%s)", type(error).__name__)
            return None
        if (
            not isinstance(vector, list)
            or not vector
            or len(vector) > 65_536
            or not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                for value in vector
            )
        ):
            self.logger.warning("embedding response is not a finite float vector")
            return None
        if len(vector) != self.dimension:
            # Kept anyway: a config mismatch is not a reason to lose the record.
            self.logger.warning(
                "embedding dimension %d differs from EMBEDDING_DIMENSION %d",
                len(vector),
                self.dimension,
            )
        norm = math.sqrt(sum(float(value) ** 2 for value in vector)) or 1.0
        return tuple(float(value) / norm for value in vector)


class LlmEntityExtractor:
    """Entity extraction by the agent's own model, one schema-bound call per note.

    The model is asked for entity text, type and confidence and never for
    offsets: offsets are the part of the answer a model invents, and locating
    the entity in the source is both how they are computed and how an invented
    entity is dropped.
    """

    def __init__(
        self,
        api_base,
        model,
        *,
        endpoint=None,
        api_key=None,
        timeout=30,
        headers=None,
        logger=None,
    ):
        if not (api_base or endpoint) or not model:
            raise CoreError("CONFIG_INVALID", "llm api base and model are required")
        # Same rule as the agent's own model client: a full URL wins over a base
        # one, because a deployment that overrides the path means it.
        self.endpoint = endpoint or api_base.rstrip("/") + "/chat/completions"
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.headers = dict(headers or {})
        self.logger = logger or logging.getLogger("core_agent.runtime")
        self.version = f"llm-ner:{model}"
        self.disabled_reason = None

    def extract(self, text):
        if self.disabled_reason:
            raise CoreError("MEMORY_PROVIDER_UNAVAILABLE", self.disabled_reason)
        response = self._request(text[:MAX_PROVIDER_TEXT])
        return {"entities": self._ground(response, text), "relations": []}

    def _request(self, text):
        try:
            return _post_json(
                self.endpoint,
                {
                    "model": self.model,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": EXTRACTION_INSTRUCTION},
                        {"role": "user", "content": text},
                    ],
                    "response_format": EXTRACTION_RESPONSE_FORMAT,
                },
                headers=self.headers,
                api_key=self.api_key,
                timeout=self.timeout,
            )
        except CoreError as error:
            status = (error.data or {}).get("status")
            # A gateway that rejects the request rejects it identically next
            # time. Retrying per memory write buys nothing and costs a model
            # round trip on the write path; 408 and 429 are the timing-dependent
            # exceptions and stay retryable.
            if isinstance(status, int) and 400 <= status < 500 and status not in (
                408,
                429,
            ):
                self.disabled_reason = f"model gateway rejected extraction ({status})"
                self.logger.warning(
                    "memory entity extraction disabled: %s", self.disabled_reason
                )
            raise

    def _ground(self, response, text):
        """Keep only entities the note actually contains, with their offsets."""
        choices = response.get("choices") if isinstance(response, dict) else None
        message = (
            choices[0].get("message")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict)
            else None
        )
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise CoreError(
                "MEMORY_PROVIDER_INVALID_RESPONSE", "extraction response has no content"
            )
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            # Schema enforcement belongs to the gateway; whether it happened is
            # not observable from here, so the answer is validated either way.
            raise CoreError(
                "MEMORY_PROVIDER_INVALID_RESPONSE", "extraction response is not JSON"
            ) from None
        entities = parsed.get("entities") if isinstance(parsed, dict) else None
        if not isinstance(entities, list):
            raise CoreError(
                "MEMORY_PROVIDER_INVALID_RESPONSE", "extraction response has no entities"
            )
        lowered = text.lower()
        grounded = []
        seen = set()
        for entity in entities[:MAX_EXTRACTED_ENTITIES]:
            if not isinstance(entity, dict):
                continue
            value = entity.get("text")
            if not isinstance(value, str) or not value.strip():
                continue
            start = lowered.find(value.lower())
            if start < 0 or value.lower() in seen:
                continue
            seen.add(value.lower())
            confidence = entity.get("confidence")
            grounded.append(
                {
                    # The note's own spelling, not the model's echo of it.
                    "text": text[start : start + len(value)],
                    "type": (
                        entity.get("type")
                        if entity.get("type") in ENTITY_TYPES
                        else "other"
                    ),
                    "start": start,
                    "end": start + len(value),
                    "confidence": (
                        float(confidence)
                        if isinstance(confidence, (int, float))
                        and not isinstance(confidence, bool)
                        and 0 <= confidence <= 1
                        else 0.5
                    ),
                }
            )
        return grounded
