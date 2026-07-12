from __future__ import annotations

import json
import math
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import MemoryServiceError


def _post_json(endpoint, payload, *, api_key=None, timeout=30, max_bytes=4_000_000):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            encoded = response.read(max_bytes + 1)
    except HTTPError as error:
        raise MemoryServiceError(
            "MEMORY_PROVIDER_UNAVAILABLE", data={"status": error.code}
        ) from None
    except (OSError, TimeoutError, URLError):
        raise MemoryServiceError("MEMORY_PROVIDER_UNAVAILABLE") from None
    if len(encoded) > max_bytes:
        raise MemoryServiceError("MEMORY_PROVIDER_INVALID_RESPONSE")
    try:
        return json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MemoryServiceError("MEMORY_PROVIDER_INVALID_RESPONSE") from None


class HttpEmbeddingProvider:
    def __init__(self, endpoint, model, *, api_key=None, timeout=30):
        if not endpoint or not model:
            raise MemoryServiceError("MEMORY_CONFIG_INVALID")
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.version = f"openai-compatible:{model}"

    def embed(self, text):
        response = _post_json(
            self.endpoint,
            {"model": self.model, "input": text},
            api_key=self.api_key,
            timeout=self.timeout,
        )
        try:
            vector = response["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError):
            raise MemoryServiceError("MEMORY_PROVIDER_INVALID_RESPONSE") from None
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
            raise MemoryServiceError("MEMORY_PROVIDER_INVALID_RESPONSE")
        norm = math.sqrt(sum(float(value) ** 2 for value in vector)) or 1.0
        return tuple(float(value) / norm for value in vector)


class HttpEntityExtractor:
    def __init__(self, endpoint, model, *, api_key=None, timeout=30):
        if not endpoint or not model:
            raise MemoryServiceError("MEMORY_CONFIG_INVALID")
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.version = f"json-ner:{model}"

    def extract(self, text):
        response = _post_json(
            self.endpoint,
            {"model": self.model, "text": text},
            api_key=self.api_key,
            timeout=self.timeout,
        )
        entities = response.get("entities") if isinstance(response, dict) else None
        relations = response.get("relations") if isinstance(response, dict) else None
        if not isinstance(entities, list) or not isinstance(relations, list):
            raise MemoryServiceError("MEMORY_PROVIDER_INVALID_RESPONSE")
        if len(entities) > 10_000 or len(relations) > 10_000:
            raise MemoryServiceError("MEMORY_PROVIDER_INVALID_RESPONSE")
        for entity in entities:
            if (
                not isinstance(entity, dict)
                or not isinstance(entity.get("text"), str)
                or not isinstance(entity.get("type"), str)
                or not isinstance(entity.get("start"), int)
                or not isinstance(entity.get("end"), int)
                or entity["start"] < 0
                or entity["end"] <= entity["start"]
                or entity["end"] > len(text)
                or not isinstance(entity.get("confidence"), (int, float))
                or isinstance(entity.get("confidence"), bool)
                or not 0 <= entity["confidence"] <= 1
            ):
                raise MemoryServiceError("MEMORY_PROVIDER_INVALID_RESPONSE")
        if not all(isinstance(relation, dict) for relation in relations):
            raise MemoryServiceError("MEMORY_PROVIDER_INVALID_RESPONSE")
        return {"entities": entities, "relations": relations}
