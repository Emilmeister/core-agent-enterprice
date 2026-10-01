"""Long-term memory as a Core Agent subsystem.

The corpus is Markdown and everything else — chunks, BM25, embeddings, entity
graph — is derived state rebuildable from it. A backend supplies durability only;
the domain rules (the hard 200-line limit, optimistic revisions, hybrid retrieval)
are identical for every backend.
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps

from .errors import CoreError
from .memory_store import StoredDocument

MAX_BODY_LINES = 200
COMPONENTS = ("markdown", "chunks", "bm25", "vectors", "ner", "graph")


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
class SearchResult:
    memory_id: str
    scores: dict
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
    results: tuple[SearchResult, ...] = ()
    component_versions: dict = field(default_factory=dict)
    candidate_set: tuple[str, ...] = ()
    degraded_channels: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "results": [item.to_dict() for item in self.results],
            "component_versions": self.component_versions,
            "candidate_set": list(self.candidate_set),
            "degraded_channels": self.degraded_channels,
        }


class RegexEntityExtractor:
    """Development extractor. A configured NER endpoint replaces it."""

    version = "builtin-ner-v2"

    def extract(self, text):
        # Capitalisation is asked of the matched word rather than encoded as a
        # latin character range, so proper names are found in every script.
        return {
            "entities": [
                {
                    "text": match.group(0),
                    "type": "proper_name",
                    "start": match.start(),
                    "end": match.end(),
                    "confidence": 0.5,
                }
                for match in re.finditer(r"\w[\w-]*", text)
                if match.group(0)[:1].isupper() and len(match.group(0)) > 1
            ],
            "relations": [],
        }


def _tokens(text):
    # `\w` is unicode-aware for str patterns; a `[a-z0-9_]` class silently
    # tokenises any non-latin script to nothing, which zeroes the query and every
    # document at once and returns an empty search over a perfectly stored note.
    return re.findall(r"\w+", text.lower())


def _serialized(function):
    @wraps(function)
    def call(self, *args, **kwargs):
        with self._lock:
            return function(self, *args, **kwargs)

    return call


def _slug(title):
    # Paths are stored strings, never filesystem names, so the title keeps its
    # own script instead of collapsing to a constant for every non-latin note.
    stem = re.sub(r"[^\w]+", "-", title.lower(), flags=re.UNICODE).strip("-_")
    return (stem or "memory")[:48]


def _scalar(name, value, *, separators=","):
    """Front matter is line-oriented, so a break inside a value is a forged key.

    The check asks `str.splitlines()` rather than enumerating characters, because
    that is exactly what every reader of this document uses. Enumerating is how
    the first version of this guard missed U+2028, U+2029 and U+0085: all three
    split a line for the parser while passing an `ord(c) < 0x20` test, which let a
    title terminate the front matter early and rewrite `kind`, `status` and the
    timestamps of an existing note.
    """
    text = str(value)
    lines = text.splitlines()
    if lines[1:] or (lines and lines[0] != text):
        raise CoreError(
            "MEMORY_INVALID", f"{name} must not contain a line break"
        )
    if any(character == "\x7f" or ord(character) < 0x20 for character in text):
        raise CoreError("MEMORY_INVALID", f"{name} must not contain control bytes")
    if any(character in text for character in separators):
        raise CoreError(
            "MEMORY_INVALID", f"{name} must not contain {separators!r}"
        )
    return text


def compose_document(
    *,
    memory_id,
    title,
    namespace,
    kind,
    status,
    body,
    tags=(),
    sources=(),
    created_at,
    updated_at,
):
    """Build the canonical Markdown the model never has to write by hand.

    Requiring correct YAML from a model produces failures it cannot diagnose, so
    the runtime owns the front matter and the model owns title and body.
    """
    title = _scalar("title", title, separators="")
    kind = _scalar("kind", kind, separators="")
    status = _scalar("status", status, separators="")
    # A tag list is comma-separated inside brackets, so those three characters
    # cannot survive the round trip and would come back as different tags.
    tags = tuple(_scalar("tag", tag, separators=",[]") for tag in tags)
    lines = [
        "---",
        f"id: {memory_id}",
        f"title: {title}",
        f"namespace: {namespace}",
        f"kind: {kind}",
        f"status: {status}",
        f"created_at: {created_at}",
        f"updated_at: {updated_at}",
        f"tags: [{', '.join(tags)}]",
        "sources:",
    ]
    for source in sources:
        lines.append(f"  - task_id: {source.get('task_id', '')}")
        lines.append(f"    event_revision: {source.get('event_revision', 0)}")
    lines.append("---")
    lines.append(body)
    return "\n".join(lines)


class MemoryService:
    """One corpus for one `(tenant_id, app_name, user_id)` scope.

    Documents live in RAM because retrieval fuses BM25, vector and graph channels
    over the whole candidate set; the store is consulted at load and at publish.
    """

    def __init__(
        self,
        store,
        *,
        app_name,
        user_id,
        tenant_id="default",
        entity_extractor=None,
        embedding_provider=None,
        telemetry=None,
        logger=None,
    ):
        self.store = store
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise CoreError("CONFIG_INVALID", "memory requires a trusted tenant")
        self.tenant_id = tenant_id
        self.app_name = app_name
        self.user_id = user_id
        # None means no model extractor is configured. The builtin still indexes
        # the corpus, but nothing it produces is worth a row in the database:
        # recomputing it costs nothing and storing it would make it
        # indistinguishable from a real extraction.
        self.extractor = entity_extractor
        self._builtin = RegexEntityExtractor()
        self.embedding_provider = embedding_provider
        self.telemetry = telemetry
        # Defaulted rather than threaded in from the app: nothing passed a
        # logger, so every warning this class raises — the quarantined row as
        # much as the failed extraction — had nowhere to go.
        self.logger = logger or logging.getLogger("core_agent.runtime")
        self._lock = threading.RLock()
        self.repository_revision = 0
        self._documents = {}
        self._paths = {}
        self._entities = {}
        self._links = {}
        self._embeddings = {}
        self._extracted = {}
        self._unreadable = ()
        self._history = {}
        self._resolutions = {}
        self._index_revision = 0
        self._channel_failures = {}
        self._load()

    def close(self):
        return None

    # ------------------------------------------------------------------ loading

    def _load(self):
        loaded = self.store.load(tenant_id=self.tenant_id, app_name=self.app_name, user_id=self.user_id)
        documents = {}
        paths = {}
        embeddings = {}
        stored_entities = {}
        unreadable = []
        for stored in loaded.documents:
            # A row this parser rejects — a document written before a validation
            # rule existed, or a poisoned one — must cost only itself. Raising
            # here made every call for that user fail forever, including the
            # delete that would have repaired it.
            document = self._parse_stored(stored, unreadable)
            if document is None:
                continue
            documents[document.id] = document
            paths[stored.path] = document.id
            if stored.embedding:
                embeddings[document.id] = stored.embedding
            # None and () differ: never extracted against extracted to nothing.
            if stored.entities is not None:
                stored_entities[document.id] = list(stored.entities)
        history = {}
        for stored in loaded.versions:
            document = self._parse_stored(stored, unreadable)
            if document is None:
                continue
            history.setdefault(document.id, []).append(document)
        self._unreadable = tuple(unreadable)
        if unreadable and self.logger:
            self.logger.warning(
                "memory skipped %d unreadable stored document(s) for %s/%s",
                len(unreadable),
                self.app_name,
                self.user_id,
            )
        for versions in history.values():
            versions.sort(key=lambda item: item.revision)
        self._documents = documents
        self._paths = paths
        self._embeddings = embeddings
        self._history = history
        self._resolutions = dict(loaded.resolutions)
        self.repository_revision = loaded.repository_revision
        self._index_revision = self.repository_revision
        # The cache survives a reload on purpose: it is keyed by content, and a
        # model extraction already paid for should not be repeated after the
        # conflict retry that brought us back here.
        self._entities, self._links = self._derive(
            documents, stored=stored_entities, extract=False
        )

    def _parse_stored(self, stored, unreadable):
        try:
            return self._parse(stored.path, stored.content, stored.revision)
        except CoreError as error:
            unreadable.append({"memory_id": stored.memory_id, "code": error.code})
            return None

    # ------------------------------------------------------------------ parsing

    def _parse(self, path, content, revision):
        lines = content.splitlines()
        if not lines or lines[0] != "---":
            raise CoreError("MEMORY_INVALID", "document must start with front matter")
        try:
            end = lines.index("---", 1)
        except ValueError:
            raise CoreError("MEMORY_INVALID", "unterminated front matter") from None
        metadata = {}
        for line in lines[1:end]:
            if line == "sources:":
                metadata["sources"] = []
                continue
            if line.startswith("  "):
                continue
            if ":" in line:
                key, value = line.split(":", 1)
                # Second guard on the same trust boundary: a repeated key is only
                # ever the result of an injected line, and resolving it silently
                # is what turns that line into a rewritten id or namespace.
                if key in metadata:
                    raise CoreError(
                        "MEMORY_INVALID", f"duplicate front matter key {key!r}"
                    )
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
            raise CoreError("MEMORY_INVALID", "front matter is missing required keys")
        body_lines = lines[end + 1 :]
        if len(body_lines) > MAX_BODY_LINES:
            raise CoreError(
                "MEMORY_FILE_TOO_LARGE",
                f"{len(body_lines)} body lines exceed the {MAX_BODY_LINES} line limit",
                data={
                    "actual_body_lines": len(body_lines),
                    "max_body_lines": MAX_BODY_LINES,
                    "recommended_action": "split_into_multiple_markdown_files",
                    "suggested_boundaries": _headings(body_lines),
                    "committed": False,
                },
            )
        return MemoryDocument(
            metadata["id"],
            metadata["title"],
            metadata["namespace"],
            metadata["kind"],
            metadata["status"],
            str(path),
            content,
            "\n".join(body_lines),
            len(body_lines),
            revision,
        )

    # ------------------------------------------------------------------ derived

    def _derive(self, documents, *, stored=None, extract=True):
        """Entity index for the corpus, calling the model only where it must.

        Each cache entry carries whether the model produced it. That flag is the
        difference between a row worth storing and one the next process should
        recompute, and it is what the degraded graph channel counts.
        """
        entities = {}
        links = {}
        with self._span("ner"):
            for document in documents.values():
                cached = self._extracted.get(document.id)
                if cached and cached[0] == document.content:
                    extracted, derived = cached[1], cached[2]
                elif stored is not None and stored.get(document.id) is not None:
                    extracted, derived = (
                        {"entities": list(stored[document.id]), "relations": []},
                        True,
                    )
                elif extract and self.extractor is not None:
                    try:
                        extracted = self.extractor.extract(
                            document.title + "\n" + document.body
                        )
                        derived = True
                    except Exception as error:
                        # BM25 and the vector channel do not depend on this, so
                        # the note stays findable either way. Refusing the write
                        # would trade the note itself for one of three channels.
                        self._log_extraction_failure(error)
                        extracted, derived = self._builtin_extract(document), False
                else:
                    # A load must never reach the model: on a scale-to-zero
                    # platform that is one call per note on every cold start.
                    extracted, derived = self._builtin_extract(document), False
                # Re-extracting every document on every write turns one tool
                # call into one model request per note in the corpus.
                self._extracted[document.id] = (document.content, extracted, derived)
                for entity in extracted.get("entities", []):
                    entities.setdefault(entity["text"], set()).add(document.id)
                links[document.id] = set(
                    re.findall(r"\[\[([^\]]+)\]\]", document.body)
                )
        for memory_id in set(self._extracted) - set(documents):
            self._extracted.pop(memory_id, None)
        return entities, links

    def _builtin_extract(self, document):
        return self._builtin.extract(document.title + "\n" + document.body)

    def _log_extraction_failure(self, error):
        if self.logger:
            code = getattr(error, "code", type(error).__name__)
            # The code, never the note: the text is the whole reason memory is
            # kept out of logs by default.
            self.logger.warning("memory entity extraction failed (%s)", code)

    def _embed(self, text):
        if self.embedding_provider is None:
            return None
        return self.embedding_provider.embed(text)

    def _span(self, name, **attributes):
        if not self.telemetry:
            return contextlib.nullcontext()
        return self.telemetry.span(
            f"core_agent.memory.{name}", attributes=attributes or None
        )

    def _degraded_channels(self):
        """Which retrieval channels answer the next search below full quality.

        Not the same as silent: a failed channel scores `null` and drops out,
        while an unconfigured extractor still ranks through the builtin regex
        one. Both are reported, because in either case the caller is reading
        scores that a configured deployment would have produced differently.
        """
        degraded = dict(self._channel_failures)
        if self.embedding_provider is None:
            degraded.setdefault("vector", "embeddings not configured")
        if self.extractor is None:
            degraded.setdefault("graph", "no entity extractor configured")
        else:
            pending = sum(
                1 for entry in self._extracted.values() if not entry[2]
            )
            if pending:
                # A count, not a list: which notes are unindexed is memory
                # content, and this string reaches the model and the operator.
                degraded.setdefault("graph", f"{pending} document(s) not extracted")
        return degraded

    def _extraction_of(self, memory_id):
        """Entities to store, or None when nothing was extracted for this note."""
        entry = self._extracted.get(memory_id)
        if entry is None or not entry[2]:
            return None
        return tuple(entry[1].get("entities", ()))

    def _commit(self, documents, paths, entities, links):
        next_revision = self.repository_revision + 1
        embeddings = dict(self._embeddings)
        with self._span("embed"):
            for document in documents.values():
                previous = self._documents.get(document.id)
                if previous is None or previous.content != document.content:
                    vector = self._embed(document.title + " " + document.body)
                    if vector:
                        embeddings[document.id] = tuple(vector)
                    else:
                        embeddings.pop(document.id, None)
        for memory_id in set(embeddings) - set(documents):
            embeddings.pop(memory_id, None)
        stored = tuple(
            StoredDocument(
                document.id,
                document.namespace,
                document.path,
                document.content,
                document.revision,
                embeddings.get(document.id),
                self._extraction_of(document.id),
            )
            for document in sorted(documents.values(), key=lambda item: item.path)
        )
        with self._span("index_publish"):
            try:
                self.store.publish(
                    tenant_id=self.tenant_id,
                    app_name=self.app_name,
                    user_id=self.user_id,
                    repository_revision=next_revision,
                    documents=stored,
                    resolutions=self._resolutions,
                )
            except CoreError as error:
                if error.code != "MEMORY_CONFLICT":
                    raise
                # Another writer moved the store on. Without reloading, this
                # instance would keep proposing the same number forever and the
                # retry the error prescribes could never succeed.
                self._load()
                raise
        self._documents = documents
        self._paths = paths
        self._entities = entities
        self._links = links
        self._embeddings = embeddings
        self.repository_revision = next_revision
        self._index_revision = next_revision
        for document in documents.values():
            versions = self._history.setdefault(document.id, [])
            if not versions or versions[-1] != document:
                versions.append(document)
        return MutationResult(True, self.repository_revision, self._index_revision)

    # ------------------------------------------------------------------ mutation

    @_serialized
    def create(self, *, title, body, namespace, kind="fact", tags=(), sources=()):
        memory_id = f"mem_{uuid.uuid4().hex[:20]}"
        now = datetime.now(timezone.utc).isoformat()
        content = compose_document(
            memory_id=memory_id,
            title=title,
            namespace=namespace,
            kind=kind,
            status="active",
            body=body,
            tags=tags,
            sources=sources,
            created_at=now,
            updated_at=now,
        )
        path = f"{namespace}/{_slug(title)}-{memory_id[-8:]}.md"
        document = self._parse(path, content, 1)
        self._require_identity(document, memory_id, namespace)
        documents = dict(self._documents)
        paths = dict(self._paths)
        documents[document.id] = document
        paths[path] = document.id
        entities, links = self._derive(documents)
        result = self._commit(documents, paths, entities, links)
        return document, result

    @_serialized
    def update(self, memory_id, *, body, expected_revision, title=None, status=None, namespace=None):
        current = self.read(memory_id, namespace=namespace)
        self._require_revision(current, expected_revision)
        content = compose_document(
            memory_id=current.id,
            title=title or current.title,
            namespace=current.namespace,
            kind=current.kind,
            status=status or current.status,
            body=body,
            # tags and sources are normative front matter; an ordinary body edit
            # must not silently drop the provenance of the note.
            tags=_front_matter_tags(current.content),
            sources=_front_matter_sources(current.content),
            created_at=_front_matter_value(current.content, "created_at"),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        updated = self._parse(current.path, content, current.revision + 1)
        self._require_identity(updated, current.id, current.namespace)
        documents = dict(self._documents)
        documents[memory_id] = updated
        entities, links = self._derive(documents)
        result = self._commit(documents, dict(self._paths), entities, links)
        return updated, result

    @_serialized
    def split(self, memory_id, *, overview, children, expected_revision, namespace=None):
        current = self.read(memory_id, namespace=namespace)
        self._require_revision(current, expected_revision)
        if not children:
            raise CoreError("MEMORY_INVALID", "split requires at least one child")
        now = datetime.now(timezone.utc).isoformat()
        created_at = _front_matter_value(current.content, "created_at")
        documents = dict(self._documents)
        paths = dict(self._paths)
        overview_content = compose_document(
            memory_id=current.id,
            title=overview.get("title") or current.title,
            namespace=current.namespace,
            kind=current.kind,
            status=current.status,
            body=overview["body"],
            tags=_front_matter_tags(current.content),
            sources=_front_matter_sources(current.content),
            created_at=created_at,
            updated_at=now,
        )
        # Every resulting document is parsed before anything is published, so an
        # oversized child rejects the whole plan instead of leaving it half applied.
        parsed = [self._parse(current.path, overview_content, current.revision + 1)]
        self._require_identity(parsed[0], current.id, current.namespace)
        for child in children:
            child_id = f"mem_{uuid.uuid4().hex[:20]}"
            child_content = compose_document(
                memory_id=child_id,
                title=child["title"],
                namespace=current.namespace,
                kind=child.get("kind") or current.kind,
                status="active",
                body=child["body"],
                created_at=now,
                updated_at=now,
            )
            child_path = (
                f"{current.namespace}/{_slug(child['title'])}-{child_id[-8:]}.md"
            )
            child_document = self._parse(child_path, child_content, 1)
            self._require_identity(child_document, child_id, current.namespace)
            parsed.append(child_document)
        for document in parsed:
            documents[document.id] = document
            paths[document.path] = document.id
        entities, links = self._derive(documents)
        result = self._commit(documents, paths, entities, links)
        return tuple(document.id for document in parsed), result

    @_serialized
    def delete(self, memory_id, *, reason, expected_revision, namespace=None):
        current = self.read(memory_id, namespace=namespace)
        self._require_revision(current, expected_revision)
        if not reason:
            raise CoreError("MEMORY_INVALID", "delete requires a reason")
        documents = dict(self._documents)
        paths = dict(self._paths)
        documents.pop(memory_id)
        paths.pop(current.path, None)
        entities, links = self._derive(documents)
        return self._commit(documents, paths, entities, links)

    @staticmethod
    def _require_identity(document, memory_id, namespace):
        """The reparsed document is only trustworthy if it is still the same one."""
        if document.id != memory_id or document.namespace != namespace:
            raise CoreError("MEMORY_INVALID", "front matter does not match the request")

    @staticmethod
    def _require_revision(current, expected_revision):
        if expected_revision != current.revision:
            raise CoreError(
                "MEMORY_CONFLICT",
                "the document changed since it was read",
                data={"current_revision": current.revision},
            )

    # ------------------------------------------------------------------- reading

    def read(self, memory_id, *, namespace=None):
        document = self._documents.get(memory_id)
        if document is None or (namespace is not None and document.namespace != namespace):
            raise CoreError("NOT_FOUND", "unknown memory id")
        return document

    def list_documents(self):
        return dict(self._documents)

    def history(self, memory_id):
        if memory_id not in self._history and memory_id not in self._documents:
            raise CoreError("NOT_FOUND", "unknown memory id")
        return tuple(self._history.get(memory_id, ()))

    def index_status(self):
        return {name: "ready" for name in COMPONENTS}

    def graph_mentions(self, entity):
        return tuple(sorted(self._entities.get(entity, set())))

    def explicit_links(self, memory_id):
        return tuple(sorted(self._links.get(memory_id, set())))

    def resolve_entity(self, name):
        return self._resolutions.get(name, f"entity:{name}")

    @_serialized
    def entity_resolve(self, source, target):
        name = source.removeprefix("entity:")
        previous = self._resolutions.get(name)
        self._resolutions[name] = target
        try:
            entities, links = self._derive(self._documents)
            return self._commit(
                dict(self._documents), dict(self._paths), entities, links
            )
        except Exception:
            if previous is None:
                self._resolutions.pop(name, None)
            else:
                self._resolutions[name] = previous
            raise

    def set_channel_available(self, channel, available, reason=None):
        if available:
            self._channel_failures.pop(channel, None)
        else:
            self._channel_failures[channel] = reason or "unavailable"

    # ------------------------------------------------------------------- search

    @_serialized
    def search(self, query, *, namespace, filters=None, limit=10):
        degraded = self._degraded_channels()
        # Embedding the query before the span opens is what lets the attribute
        # name the degradation this search discovered. Computing it beforehand
        # reported an empty set for the one case that actually matters: a
        # configured endpoint that fails at query time.
        query_vector = self._embed(query)
        if self.embedding_provider is not None and query_vector is None:
            degraded["vector"] = "embedding unavailable"
        # Channel names only: the reason is operator text and the query never is.
        attributes = {
            "core_agent.memory.degraded_channels": ",".join(sorted(degraded))
        }
        with self._span("search", **attributes):
            return self._search(
                query,
                namespace=namespace,
                filters=filters,
                limit=limit,
                query_vector=query_vector,
                degraded=degraded,
            )

    def _vector_scores(self, namespace, query_vector, limit):
        """Ask the backend to rank by distance; fall back to in-process cosine."""
        if query_vector is None:
            # The span still opens: a missing channel is part of the trace.
            with self._span("search.vector"):
                return None
        ranker = getattr(self.store, "vector_candidates", None)
        if ranker is not None:
            with self._span("search.vector"):
                ranked = ranker(
                    tenant_id=self.tenant_id,
                    app_name=self.app_name,
                    user_id=self.user_id,
                    namespace=namespace,
                    embedding=query_vector,
                    limit=max(limit * 5, 50),
                )
            if ranked is not None:
                return ranked
        with self._span("search.vector"):
            scores = {}
            for memory_id, vector in self._embeddings.items():
                if len(vector) != len(query_vector):
                    continue
                scores[memory_id] = sum(
                    left * right for left, right in zip(vector, query_vector)
                )
            return scores

    def _search(
        self, query, *, namespace, filters=None, limit=10, query_vector=None,
        degraded=None,
    ):
        filters = filters or {}
        degraded = dict(degraded or self._degraded_channels())
        candidates = [
            document
            for document in self._documents.values()
            if document.namespace == namespace
            and (not filters.get("kind") or document.kind == filters["kind"])
            and (not filters.get("status") or document.status == filters["status"])
        ]
        query_terms = _tokens(query)
        query_tokens = set(query_terms)
        with self._span("search.bm25"):
            tokenized = {
                document.id: _tokens(document.title + " " + document.body)
                for document in candidates
            }
            average_length = sum(map(len, tokenized.values())) / max(1, len(tokenized))
            document_frequency = {
                term: sum(term in tokens for tokens in tokenized.values())
                for term in query_tokens
            }
        vector_scores = self._vector_scores(namespace, query_vector, limit) or {}
        scored = []
        with self._span("search.graph"):
            # Entities are indexed by their tokens, not by the whole string: a
            # model returns "Cloud.ru ML Inference", and comparing that to a
            # query token matches only if the entire phrase is repeated
            # verbatim — the channel would go quiet exactly when extraction
            # starts working well.
            entity_index = {
                document.id: {
                    entity: frozenset(_tokens(entity))
                    for entity, ids in self._entities.items()
                    if document.id in ids
                }
                for document in candidates
            }
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
            vector = None if query_vector is None else vector_scores.get(document.id, 0.0)
            mentioned = {
                entity: tokens & query_tokens
                for entity, tokens in entity_index[document.id].items()
            }
            matched = set().union(*mentioned.values()) if mentioned else set()
            graph = (
                None
                if "graph" in self._channel_failures
                # Still the share of the query that is a known mention, so a
                # long entity does not outweigh a short one.
                else len(matched) / max(1, len(query_tokens))
            )
            preliminary = bm25 + (vector or 0) + (graph or 0)
            if preliminary > 0:
                paths = tuple(
                    f"query->{entity}->{document.id}"
                    for entity in sorted(entity for entity, hit in mentioned.items() if hit)
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
        with self._span("search.rerank"):
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
                scores["final"] = sum(
                    1 / (60 + ranks[document.id])
                    for ranks in channel_ranks.values()
                    if document.id in ranks
                )
                fused.append((scores["final"], document, scores, paths))
            scored = sorted(fused, key=lambda item: (-item[0], item[1].id))
        results = tuple(
            SearchResult(
                document.id,
                scores,
                paths,
                self.repository_revision,
                {"path": document.path, "title": document.title},
            )
            for _, document, scores, paths in scored[:limit]
        )
        versions = {
            "bm25": "bm25-okapi-v2",
            "embedding": getattr(self.embedding_provider, "version", "disabled"),
            "graph": "builtin-v1",
            "reranker": "rrf-v1",
            # The extractor that produced this index, which is the builtin one
            # whenever no model extractor is configured.
            "ner": getattr(self.extractor or self._builtin, "version", "unknown"),
        }
        return SearchResponse(
            results,
            versions,
            tuple(item[1].id for item in scored),
            degraded,
        )

    @_serialized
    def rebuild(self):
        self._entities, self._links = self._derive(self._documents)
        self._index_revision = self.repository_revision


def _headings(body_lines):
    found = [line.lstrip("# ").strip() for line in body_lines if line.startswith("#")]
    return found[:8] or [str(MAX_BODY_LINES)]


def _front_matter_value(content, key, default=None):
    for line in content.splitlines()[1:]:
        if line == "---":
            break
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return datetime.now(timezone.utc).isoformat() if default is None else default


def _front_matter_tags(content):
    raw = _front_matter_value(content, "tags", default="").strip()
    return tuple(
        item.strip() for item in raw.strip("[]").split(",") if item.strip()
    )


def _front_matter_sources(content):
    sources = []
    inside = False
    for line in content.splitlines()[1:]:
        if line == "---":
            break
        if line == "sources:":
            inside = True
            continue
        if not inside:
            continue
        stripped = line.strip()
        if stripped.startswith("- task_id:"):
            sources.append(
                {"task_id": stripped.split(":", 1)[1].strip(), "event_revision": 0}
            )
        elif stripped.startswith("event_revision:") and sources:
            value = stripped.split(":", 1)[1].strip()
            sources[-1]["event_revision"] = int(value) if value.isdigit() else 0
        elif not line.startswith(" "):
            inside = False
    return tuple(sources)


class MemoryRegistry:
    """One service per `(tenant_id, app_name, user_id)` scope, created on first use.

    Memory is per-user by contract, and a single corpus in RAM for every user of
    a multi-tenant deployment would both leak data across scopes and make the
    repository revision meaningless.
    """

    def __init__(
        self,
        store,
        *,
        entity_extractor=None,
        embedding_provider=None,
        telemetry=None,
        search_limit=10,
        logger=None,
    ):
        self.store = store
        self.entity_extractor = entity_extractor
        self.embedding_provider = embedding_provider
        self.telemetry = telemetry
        self.search_limit = int(search_limit)
        self.logger = logger
        self._services = {}
        self._lock = threading.RLock()

    def service(self, app_name, user_id, *, tenant_id="default"):
        key = (tenant_id, app_name, user_id)
        with self._lock:
            service = self._services.get(key)
            if service is None:
                service = MemoryService(
                    self.store,
                    tenant_id=tenant_id,
                    app_name=app_name,
                    user_id=user_id,
                    entity_extractor=self.entity_extractor,
                    embedding_provider=self.embedding_provider,
                    telemetry=self.telemetry,
                    logger=self.logger,
                )
                self._services[key] = service
            return service

    def close(self):
        with self._lock:
            for service in self._services.values():
                service.close()
            self._services.clear()
        self.store.close()
