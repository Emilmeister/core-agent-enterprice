from __future__ import annotations

import contextlib
import math
import re
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import MemoryServiceError


@dataclass(frozen=True)
class MemoryDocument:
    id: str
    title: str
    namespace: str
    kind: str
    status: str
    path: str
    content: str
    body: str
    body_line_count: int
    revision: int


@dataclass(frozen=True)
class MutationResult:
    committed: bool
    repository_revision: int
    index_revision: int


@dataclass(frozen=True)
class IndexStatus:
    components: dict[str, str]


@dataclass(frozen=True)
class SearchResult:
    memory_id: str
    scores: dict[str, float | None]
    graph_paths: tuple[str, ...]
    revision: int
    provenance: dict

    def to_dict(self):
        return {
            "memory_id": self.memory_id,
            "scores": self.scores,
            "graph_paths": list(self.graph_paths),
            "revision": self.revision,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class SearchResponse:
    results: tuple[SearchResult, ...]
    component_versions: dict[str, str]
    candidate_set: tuple[str, ...]
    degraded_channels: dict[str, str]

    def to_dict(self):
        return {
            "results": [item.to_dict() for item in self.results],
            "component_versions": self.component_versions,
            "candidate_set": list(self.candidate_set),
            "degraded_channels": self.degraded_channels,
        }


class _DefaultExtractor:
    version = "builtin-ner-v1"

    def extract(self, text):
        return {
            "entities": [
                {
                    "text": match.group(0),
                    "type": "proper_name",
                    "start": match.start(),
                    "end": match.end(),
                    "confidence": 0.5,
                }
                for match in re.finditer(r"\b[A-Z][A-Za-z0-9_-]+\b", text)
            ],
            "relations": [],
        }


class _HashEmbeddingProvider:
    version = "hash-embedding-v1"

    def embed(self, text):
        vector = [0.0] * 256
        for token in _tokens(text):
            digest = hashlib.blake2b(token.encode(), digest_size=4).digest()
            index = int.from_bytes(digest, "big") % len(vector)
            vector[index] += 1.0 if digest[0] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return tuple(value / norm for value in vector)


def _tokens(text):
    return re.findall(r"[a-z0-9_]+", text.lower())


class MemoryService:
    COMPONENTS = ("markdown", "chunks", "bm25", "vectors", "ner", "graph")

    def __init__(
        self,
        root,
        entity_extractor=None,
        embedding_provider=None,
        telemetry=None,
        namespace_authorizer=None,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.extractor = entity_extractor or _DefaultExtractor()
        self.embedding_provider = embedding_provider or _HashEmbeddingProvider()
        self.telemetry = telemetry
        self.namespace_authorizer = namespace_authorizer
        self.repository_revision = 0
        self._documents = {}
        self._paths = {}
        self._entities = {}
        self._links = {}
        self._index_revision = 0
        self._status = {}
        self._channel_failures = {}
        self._resolutions = {}
        self._history = {}
        self._load_from_markdown()

    def close(self):
        pass

    def _authorize_namespace(self, namespace, action):
        if self.namespace_authorizer and not self.namespace_authorizer(
            namespace, action
        ):
            raise MemoryServiceError("NOT_FOUND")

    def _load_from_markdown(self):
        documents = {}
        paths = {}
        for path in sorted(self.root.rglob("*.md")):
            try:
                relative = str(path.relative_to(self.root))
                content = path.read_text(encoding="utf-8")
                draft = self._parse(relative, content, 1)
                previous = self._documents.get(draft.id)
                revision = (
                    previous.revision
                    if previous and previous.content == content
                    else (previous.revision + 1 if previous else 1)
                )
                document = replace(draft, revision=revision)
            except (OSError, MemoryServiceError) as error:
                raise MemoryServiceError("MEMORY_INDEX_FAILED", str(error)) from error
            if document.id in documents:
                raise MemoryServiceError("MEMORY_INDEX_FAILED", "duplicate memory id")
            documents[document.id] = document
            paths[relative] = document.id
        entities, links = self._derive(documents)
        self._documents, self._paths = documents, paths
        self._entities, self._links = entities, links
        if documents and self.repository_revision == 0:
            self.repository_revision = 1
        self._index_revision = self.repository_revision
        if self.repository_revision:
            self._status[self.repository_revision] = IndexStatus(
                {name: "ready" for name in self.COMPONENTS}
            )
        for document in documents.values():
            versions = self._history.setdefault(document.id, [])
            if not versions or versions[-1] != document:
                versions.append(document)

    def _parse(self, path, content, revision):
        lines = content.splitlines()
        if not lines or lines[0] != "---":
            raise MemoryServiceError("MEMORY_INVALID")
        try:
            end = lines.index("---", 1)
        except ValueError:
            raise MemoryServiceError("MEMORY_INVALID") from None
        metadata = {}
        for line in lines[1:end]:
            if line == "sources:":
                metadata["sources"] = []
                continue
            if line.startswith("  "):
                continue
            if ":" in line:
                key, value = line.split(":", 1)
                metadata[key] = value.strip()
        required = {
            "id",
            "title",
            "namespace",
            "kind",
            "status",
            "created_at",
            "updated_at",
            "sources",
        }
        if not required <= set(metadata):
            raise MemoryServiceError("MEMORY_INVALID")
        self._authorize_namespace(metadata["namespace"], "write")
        body_lines = lines[end + 1 :]
        if len(body_lines) > 200:
            raise MemoryServiceError(
                "MEMORY_FILE_TOO_LARGE",
                data={
                    "actual_body_lines": len(body_lines),
                    "max_body_lines": 200,
                    "recommended_action": "split_into_multiple_markdown_files",
                    "suggested_boundaries": [200],
                    "committed": False,
                },
            )
        body = "\n".join(body_lines)
        return MemoryDocument(
            metadata["id"],
            metadata["title"],
            metadata["namespace"],
            metadata["kind"],
            metadata["status"],
            str(path),
            content,
            body,
            len(body_lines),
            revision,
        )

    def _safe_path(self, relative):
        candidate = (self.root / relative).resolve(strict=False)
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError:
            raise MemoryServiceError("POLICY_DENIED") from None
        return candidate

    def _derive(self, documents):
        entities = {}
        links = {}
        for document in documents.values():
            try:
                extracted = self.extractor.extract(
                    document.title + "\n" + document.body
                )
            except Exception as error:
                raise MemoryServiceError(
                    "MEMORY_INDEX_FAILED", str(error), data={"committed": False}
                ) from error
            for entity in extracted.get("entities", []):
                entities.setdefault(entity["text"], set()).add(document.id)
            links[document.id] = set(re.findall(r"\[\[([^\]]+)\]\]", document.body))
        return entities, links

    def _commit(self, documents, paths, entities, links, writes, deletes=()):
        for relative, content in writes:
            path = self._safe_path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        for relative in deletes:
            path = self._safe_path(relative)
            if path.exists():
                path.unlink()
        self._documents = documents
        self._paths = paths
        self._entities = entities
        self._links = links
        self.repository_revision += 1
        self._index_revision = self.repository_revision
        self._status[self.repository_revision] = IndexStatus(
            {name: "ready" for name in self.COMPONENTS}
        )
        for document in documents.values():
            versions = self._history.setdefault(document.id, [])
            if not versions or versions[-1] != document:
                versions.append(document)
        return MutationResult(True, self.repository_revision, self._index_revision)

    def create(
        self, path, content, expected_repository_revision=None, *, trace_carrier=None
    ):
        if expected_repository_revision is None:
            raise MemoryServiceError(
                "MEMORY_CONFLICT", data={"current_revision": self.repository_revision}
            )
        parent = self.telemetry.extract(trace_carrier or {}) if self.telemetry else None
        request_span = (
            self.telemetry.span("memory_service.mcp.request", parent=parent)
            if self.telemetry
            else contextlib.nullcontext()
        )
        with request_span:
            return self._create(path, content, expected_repository_revision)

    def _create(self, path, content, expected_repository_revision):
        self._safe_path(path)
        if expected_repository_revision != self.repository_revision:
            raise MemoryServiceError(
                "MEMORY_CONFLICT", data={"current_revision": self.repository_revision}
            )
        document = self._parse(path, content, 1)
        if document.id in self._documents or path in self._paths:
            raise MemoryServiceError(
                "MEMORY_CONFLICT", data={"current_revision": self.repository_revision}
            )
        documents = dict(self._documents)
        paths = dict(self._paths)
        documents[document.id] = document
        paths[path] = document.id
        if self.telemetry:
            with contextlib.ExitStack() as stack:
                for name in (
                    "chunking",
                    "bm25",
                    "embeddings",
                    "ner",
                    "entity_resolution",
                    "graph",
                ):
                    stack.enter_context(self.telemetry.span(f"memory_service.{name}"))
                entities, links = self._derive(documents)
        else:
            entities, links = self._derive(documents)
        publish_span = (
            self.telemetry.span("memory_service.index_publish")
            if self.telemetry
            else contextlib.nullcontext()
        )
        with publish_span:
            return self._commit(documents, paths, entities, links, [(path, content)])

    def read(self, memory_id):
        try:
            document = self._documents[memory_id]
        except KeyError:
            raise MemoryServiceError("NOT_FOUND") from None
        self._authorize_namespace(document.namespace, "read")
        return document

    def read_id_or_path(self, id_or_path):
        memory_id = self._paths.get(id_or_path, id_or_path)
        return self.read(memory_id)

    def list_documents(self):
        return tuple(sorted(self._documents.values(), key=lambda item: item.path))

    def update(self, memory_id, patch, *, expected_file_revision):
        current = self.read(memory_id)
        if expected_file_revision != current.revision:
            raise MemoryServiceError(
                "MEMORY_CONFLICT", data={"current_revision": current.revision}
            )
        content = patch.get("replace_content")
        if not isinstance(content, str):
            raise MemoryServiceError("MEMORY_INVALID")
        updated = self._parse(current.path, content, current.revision + 1)
        if updated.id != memory_id:
            raise MemoryServiceError("MEMORY_INVALID")
        documents = dict(self._documents)
        documents[memory_id] = updated
        entities, links = self._derive(documents)
        return self._commit(
            documents, dict(self._paths), entities, links, [(current.path, content)]
        )

    def split(self, memory_id, plan, *, expected_file_revision):
        current = self.read(memory_id)
        if expected_file_revision != current.revision:
            raise MemoryServiceError(
                "MEMORY_CONFLICT", data={"current_revision": current.revision}
            )
        entries = [plan["overview"], *plan.get("children", [])]
        parsed = []
        for entry in entries:
            existing = (
                self._documents.get(memory_id) if entry is plan["overview"] else None
            )
            parsed.append(
                self._parse(
                    entry["path"],
                    entry["content"],
                    (existing.revision + 1) if existing else 1,
                )
            )
        if parsed[0].id != memory_id or len({item.id for item in parsed}) != len(
            parsed
        ):
            raise MemoryServiceError("MEMORY_INVALID")
        documents = dict(self._documents)
        paths = dict(self._paths)
        old_path = current.path
        paths.pop(old_path, None)
        for document in parsed:
            documents[document.id] = document
            paths[document.path] = document.id
        entities, links = self._derive(documents)
        deletes = () if old_path in {item.path for item in parsed} else (old_path,)
        return self._commit(
            documents,
            paths,
            entities,
            links,
            [(item.path, item.content) for item in parsed],
            deletes,
        )

    def delete(self, memory_id, reason, *, expected_file_revision):
        current = self.read(memory_id)
        if expected_file_revision != current.revision:
            raise MemoryServiceError(
                "MEMORY_CONFLICT", data={"current_revision": current.revision}
            )
        documents = dict(self._documents)
        paths = dict(self._paths)
        documents.pop(memory_id)
        paths.pop(current.path, None)
        entities, links = self._derive(documents)
        return self._commit(documents, paths, entities, links, [], (current.path,))

    def move(self, memory_id, new_path, *, expected_file_revision):
        current = self.read(memory_id)
        if expected_file_revision != current.revision:
            raise MemoryServiceError(
                "MEMORY_CONFLICT", data={"current_revision": current.revision}
            )
        self._safe_path(new_path)
        if new_path in self._paths:
            raise MemoryServiceError(
                "MEMORY_CONFLICT", data={"current_revision": current.revision}
            )
        moved = MemoryDocument(
            current.id,
            current.title,
            current.namespace,
            current.kind,
            current.status,
            new_path,
            current.content,
            current.body,
            current.body_line_count,
            current.revision + 1,
        )
        documents = dict(self._documents)
        documents[memory_id] = moved
        paths = dict(self._paths)
        paths.pop(current.path)
        paths[new_path] = memory_id
        entities, links = self._derive(documents)
        return self._commit(
            documents,
            paths,
            entities,
            links,
            [(new_path, current.content)],
            (current.path,),
        )

    def history(self, memory_id):
        if memory_id not in self._history and memory_id not in self._documents:
            raise MemoryServiceError("NOT_FOUND")
        return tuple(self._history.get(memory_id, ()))

    def index_status(self, revision):
        return self._status[revision]

    def graph_mentions(self, entity):
        return tuple(sorted(self._entities.get(entity, set())))

    def explicit_links(self, memory_id):
        return tuple(sorted(self._links.get(memory_id, set())))

    def index_snapshot(self):
        return {
            "revision": self._index_revision,
            "documents": {key: value for key, value in sorted(self._documents.items())},
            "entities": {
                key: tuple(sorted(value))
                for key, value in sorted(self._entities.items())
            },
            "links": {
                key: tuple(sorted(value)) for key, value in sorted(self._links.items())
            },
        }

    def set_channel_available(self, channel, available, reason=None):
        if available:
            self._channel_failures.pop(channel, None)
        else:
            self._channel_failures[channel] = reason or "unavailable"

    def search(self, query, *, namespace, filters=None, limit=10, trace_carrier=None):
        if not self.telemetry:
            return self._search(
                query, namespace=namespace, filters=filters, limit=limit
            )
        parent = self.telemetry.extract(trace_carrier or {})
        request_span = (
            "memory_service.mcp.request"
            if trace_carrier is not None
            else "memory_service.search"
        )
        with self.telemetry.span(request_span, parent=parent):
            with contextlib.ExitStack() as stack:
                for name in ("bm25", "vector", "query_ner", "graph", "rerank"):
                    stack.enter_context(self.telemetry.span(f"memory_service.{name}"))
                return self._search(
                    query, namespace=namespace, filters=filters, limit=limit
                )

    def _search(self, query, *, namespace, filters=None, limit=10):
        self._authorize_namespace(namespace, "search")
        query_terms = _tokens(query)
        query_tokens = set(query_terms)
        filters = filters or {}
        candidates = [
            document
            for document in self._documents.values()
            if document.namespace == namespace
            and (not filters.get("kind") or document.kind == filters["kind"])
            and (not filters.get("status") or document.status == filters["status"])
        ]
        tokenized = {
            document.id: _tokens(document.title + " " + document.body)
            for document in candidates
        }
        average_length = sum(map(len, tokenized.values())) / max(1, len(tokenized))
        document_frequency = {
            term: sum(term in tokens for tokens in tokenized.values())
            for term in query_tokens
        }
        query_vector = self.embedding_provider.embed(query)
        scored = []
        for document in candidates:
            terms = tokenized[document.id]
            bm25 = 0.0
            for term in query_terms:
                frequency = terms.count(term)
                if not frequency:
                    continue
                idf = math.log(
                    1
                    + (len(candidates) - document_frequency[term] + 0.5)
                    / (document_frequency[term] + 0.5)
                )
                denominator = frequency + 1.2 * (
                    1 - 0.75 + 0.75 * len(terms) / max(1, average_length)
                )
                bm25 += idf * frequency * 2.2 / denominator
            document_vector = self.embedding_provider.embed(
                document.title + " " + document.body
            )
            vector = sum(
                left * right
                for left, right in zip(query_vector, document_vector, strict=True)
            )
            entity_names = {
                entity.lower()
                for entity, ids in self._entities.items()
                if document.id in ids
            }
            graph = (
                None
                if "graph" in self._channel_failures
                else (len(query_tokens & entity_names) / max(1, len(query_tokens)))
            )
            preliminary = bm25 + vector + (graph or 0)
            if preliminary > 0:
                paths = tuple(
                    f"query->{entity}->{document.id}"
                    for entity in sorted(entity_names)
                    if entity in query_tokens
                )
                scored.append(
                    (
                        preliminary,
                        document,
                        {"bm25": bm25, "vector": vector, "graph": graph},
                        paths,
                    )
                )
        scored.sort(key=lambda item: (-item[0], item[1].id))
        channel_ranks = {}
        for channel in ("bm25", "vector", "graph"):
            ranked = sorted(
                scored, key=lambda item: (-(item[2][channel] or 0), item[1].id)
            )
            channel_ranks[channel] = {
                item[1].id: rank
                for rank, item in enumerate(ranked, 1)
                if item[2][channel] is not None
            }
        fused = []
        for _, document, scores, paths in scored:
            final = sum(
                1 / (60 + ranks[document.id])
                for ranks in channel_ranks.values()
                if document.id in ranks
            )
            scores["final"] = final
            fused.append((final, document, scores, paths))
        scored = sorted(fused, key=lambda item: (-item[0], item[1].id))
        candidate_set = tuple(item[1].id for item in scored)
        results = tuple(
            SearchResult(
                document.id,
                scores,
                paths,
                self.repository_revision,
                {"path": document.path},
            )
            for _, document, scores, paths in scored[:limit]
        )
        versions = {
            "bm25": "bm25-okapi-v1",
            "embedding": getattr(self.embedding_provider, "version", "unknown"),
            "graph": "builtin-v1",
            "reranker": "weighted-v1",
            "ner": getattr(self.extractor, "version", "unknown"),
        }
        return SearchResponse(
            results, versions, candidate_set, dict(self._channel_failures)
        )

    def drop_derived_indexes(self):
        self._entities = {}
        self._links = {}
        self._index_revision = 0

    def rebuild(self):
        self._load_from_markdown()
        self._entities, self._links = self._derive(self._documents)
        self._index_revision = self.repository_revision

    def entity_resolve(self, source, target, *, evidence):
        self._resolutions[source.removeprefix("entity:")] = target

    def resolve_entity(self, name):
        return self._resolutions.get(name, f"entity:{name}")
