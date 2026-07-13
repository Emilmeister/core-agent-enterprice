from __future__ import annotations

import dataclasses
import json
import os
from functools import wraps
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .errors import MemoryServiceError
from .providers import HttpEmbeddingProvider, HttpEntityExtractor
from .service import MemoryService


INSTRUCTIONS = """Memory is Markdown-first. Search before every mutation and inspect top candidates.
Update an existing file for the same topic; create only for a new subject or scope.
A Markdown body may contain at most 200 lines. An oversized write is rejected and must be
followed by a separate explicit memory.split call with a semantic split plan."""


def _dict(value):
    if dataclasses.is_dataclass(value):
        return {key: _dict(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {key: _dict(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_dict(item) for item in value]
    return value


def _guard(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return _dict(function(*args, **kwargs))
        except MemoryServiceError as error:
            raise ToolError(
                json.dumps(
                    {
                        "code": error.code,
                        "message": error.message,
                        "data": error.data,
                    },
                    sort_keys=True,
                )
            ) from error

    return call


def _trace_carrier(context):
    request = context.request_context.request
    meta = request.params.meta if request and request.params else None
    value = (meta.model_extra or {}).get("traceparent") if meta else None
    return {"traceparent": value} if isinstance(value, str) else None


def build_memory_mcp(service, *, host="127.0.0.1", port=8000):
    mcp = FastMCP(
        "core-agent-memory",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        host=host,
        port=port,
    )

    @mcp.tool(name="memory.search")
    @_guard
    def memory_search(
        context: Context,
        query: str,
        namespace: str,
        filters: dict | None = None,
        limit: int = 10,
    ) -> dict:
        """Hybrid BM25, embedding and entity-graph retrieval."""
        return service.search(
            query,
            namespace=namespace,
            filters=filters,
            limit=limit,
            trace_carrier=_trace_carrier(context),
        )

    @mcp.tool(name="memory.read")
    @_guard
    def memory_read(
        id_or_path: str, revision: int | None = None, section: str | None = None
    ) -> dict:
        """Read a committed Markdown memory by stable id or path."""
        document = service.read_id_or_path(id_or_path)
        if revision is not None and document.revision != revision:
            raise MemoryServiceError(
                "MEMORY_CONFLICT", data={"current_revision": document.revision}
            )
        return document

    @mcp.tool(name="memory.create")
    @_guard
    def memory_create(
        context: Context,
        path: str,
        content: str,
        expected_repository_revision: int,
    ) -> dict:
        """Atomically create and index a new Markdown memory."""
        return service.create(
            path,
            content,
            expected_repository_revision,
            trace_carrier=_trace_carrier(context),
        )

    @mcp.tool(name="memory.update")
    @_guard
    def memory_update(memory_id: str, patch: dict, expected_file_revision: int) -> dict:
        """Atomically update and reindex an existing memory."""
        return service.update(
            memory_id, patch, expected_file_revision=expected_file_revision
        )

    @mcp.tool(name="memory.split")
    @_guard
    def memory_split(memory_id: str, plan: dict, expected_file_revision: int) -> dict:
        """Apply an explicit atomic semantic split plan."""
        return service.split(
            memory_id, plan, expected_file_revision=expected_file_revision
        )

    @mcp.tool(name="memory.move")
    @_guard
    def memory_move(memory_id: str, new_path: str, expected_file_revision: int) -> dict:
        """Move a memory while preserving its stable id."""
        return service.move(
            memory_id, new_path, expected_file_revision=expected_file_revision
        )

    @mcp.tool(name="memory.delete")
    @_guard
    def memory_delete(memory_id: str, reason: str, expected_file_revision: int) -> dict:
        """Delete canonical Markdown and all derived data."""
        return service.delete(
            memory_id, reason, expected_file_revision=expected_file_revision
        )

    @mcp.tool(name="memory.history")
    @_guard
    def memory_history(memory_id: str) -> list[dict]:
        """Return committed file revisions."""
        return service.history(memory_id)

    @mcp.tool(name="memory.index_status")
    @_guard
    def memory_index_status(revision: int) -> dict:
        """Return component publication status for a repository revision."""
        try:
            return service.index_status(revision)
        except KeyError as error:
            raise MemoryServiceError("NOT_FOUND") from error

    @mcp.tool(name="memory.entity_resolve")
    @_guard
    def memory_entity_resolve(entity_id: str, target_id: str, evidence: str) -> dict:
        """Persist a manual entity-resolution correction."""
        service.entity_resolve(entity_id, target_id, evidence=evidence)
        return {"committed": True, "target_id": target_id}

    return mcp


def main():
    environment = os.getenv("MEMORY_ENVIRONMENT", "development")
    root = os.getenv("MEMORY_ROOT", "")
    prefixes = tuple(
        value.strip()
        for value in os.getenv("MEMORY_ALLOWED_NAMESPACE_PREFIXES", "").split(",")
        if value.strip()
    )
    embedding_endpoint = os.getenv("MEMORY_EMBEDDING_ENDPOINT", "")
    embedding_model = os.getenv("MEMORY_EMBEDDING_MODEL", "")
    ner_endpoint = os.getenv("MEMORY_NER_ENDPOINT", "")
    ner_model = os.getenv("MEMORY_NER_MODEL", "")
    if environment == "production" and not all(
        (root, prefixes, embedding_endpoint, embedding_model, ner_endpoint, ner_model)
    ):
        raise MemoryServiceError("MEMORY_CONFIG_INVALID")
    try:
        timeout = float(os.getenv("MEMORY_PROVIDER_TIMEOUT_SECONDS", "30"))
        port = int(os.getenv("MEMORY_PORT", "8000"))
    except ValueError:
        raise MemoryServiceError("MEMORY_CONFIG_INVALID") from None
    if timeout <= 0 or not 1 <= port <= 65_535:
        raise MemoryServiceError("MEMORY_CONFIG_INVALID")
    embedding = (
        HttpEmbeddingProvider(
            embedding_endpoint,
            embedding_model,
            api_key=os.getenv("MEMORY_EMBEDDING_API_KEY"),
            timeout=timeout,
        )
        if embedding_endpoint or embedding_model
        else None
    )
    extractor = (
        HttpEntityExtractor(
            ner_endpoint,
            ner_model,
            api_key=os.getenv("MEMORY_NER_API_KEY"),
            timeout=timeout,
        )
        if ner_endpoint or ner_model
        else None
    )

    def authorize(namespace, _action):
        return not prefixes or any(namespace.startswith(prefix) for prefix in prefixes)

    telemetry = None
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from core_agent.observability import Telemetry

        telemetry = Telemetry.otlp(
            endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"],
            service_name="core-agent-memory",
        )
    service = MemoryService(
        Path(root or "./memory"),
        entity_extractor=extractor,
        embedding_provider=embedding,
        telemetry=telemetry,
        namespace_authorizer=authorize,
    )
    try:
        build_memory_mcp(
            service,
            host=os.getenv("MEMORY_HOST", "127.0.0.1"),
            port=port,
        ).run(transport="streamable-http")
    finally:
        service.close()
        if telemetry:
            telemetry.shutdown()


if __name__ == "__main__":
    main()
