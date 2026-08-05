"""Storage backends for the in-agent memory subsystem.

The domain keeps its corpus in RAM and uses a store only for durability, so a
backend implements two operations: load everything for one scope, and publish the
next repository revision atomically. `vector_candidates` is optional — a backend
that cannot rank by distance simply lets the service compute cosine in process.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from psycopg import errors

from .errors import CoreError


@dataclass(frozen=True)
class StoredDocument:
    """One canonical Markdown document as the backend keeps it.

    `content` is the whole document including front matter: kind, status, sources
    and timestamps are part of the canonical text and must survive a restart
    without being reconstructed.
    """

    memory_id: str
    namespace: str
    path: str
    content: str
    revision: int
    embedding: tuple[float, ...] | None = None
    # None is "never extracted" and () is "extracted to nothing". Only the first
    # degrades the graph channel, so the two must not collapse into one value.
    entities: tuple[dict, ...] | None = None


@dataclass(frozen=True)
class LoadedMemory:
    documents: tuple[StoredDocument, ...] = ()
    versions: tuple[StoredDocument, ...] = ()
    resolutions: dict = field(default_factory=dict)
    repository_revision: int = 0


class InMemoryMemoryStore:
    """Development backend: everything lives in the process and dies with it."""

    def __init__(self):
        self._scopes = {}

    def load(self, *, app_name, user_id):
        return self._scopes.get((app_name, user_id), LoadedMemory())

    def publish(
        self, *, app_name, user_id, repository_revision, documents, resolutions
    ):
        key = (app_name, user_id)
        current = self._scopes.get(key, LoadedMemory())
        if repository_revision <= current.repository_revision:
            raise CoreError(
                "MEMORY_CONFLICT",
                "repository revision already published",
                data={"current_revision": current.repository_revision},
            )
        seen = {(item.memory_id, item.revision) for item in current.versions}
        versions = current.versions + tuple(
            item for item in documents if (item.memory_id, item.revision) not in seen
        )
        self._scopes[key] = LoadedMemory(
            documents=tuple(documents),
            versions=versions,
            resolutions=dict(resolutions),
            repository_revision=repository_revision,
        )

    def close(self):
        self._scopes.clear()


class PostgresMemoryStore:
    """Durable backend on the agent's shared pool.

    Publication is one transaction: the revision row, the document upserts, the
    version inserts and the tombstone deletes become visible together or not at
    all. The primary key of `core_memory_revisions` is what makes a second replica
    publishing the same revision number fail instead of silently overwriting.
    """

    def __init__(self, database, *, embedding_dimension=768, logger=None):
        self.database = database
        self.embedding_dimension = int(embedding_dimension)
        self.logger = logger or logging.getLogger("core_agent.runtime")
        self.vector_index_available = self._prepare_vector_index()

    def _prepare_vector_index(self):
        """Best effort: pgvector buys an index, never the ability to store a vector.

        The mandatory migrations run as one transaction, so a failing CREATE
        EXTENSION there would roll back the whole schema. Managed PostgreSQL
        without the extension must still start and search on the other channels.
        """
        dimension = self.embedding_dimension
        statements = (
            "CREATE EXTENSION IF NOT EXISTS vector",
            # The dimension is in the name because a partial expression index is
            # pinned to it: reusing one name across dimensions would silently keep
            # the old index after EMBEDDING_DIMENSION changed. And the length is in
            # the predicate because the cast is evaluated for every row the index
            # covers — without it a vector of another length is not merely
            # unindexed, it is unwritable, which contradicts the rule that a
            # dimension mismatch is a warning and not a lost record.
            f"CREATE INDEX IF NOT EXISTS core_memory_embedding_{dimension}_idx "
            "ON core_memory_documents USING hnsw "
            f"((embedding::vector({dimension})) vector_cosine_ops) "
            "WHERE embedding IS NOT NULL "
            f"AND array_length(embedding, 1) = {dimension}",
        )
        for statement in statements:
            try:
                with self.database.transaction() as connection:
                    connection.execute(statement)
            except Exception as error:
                self.logger.warning(
                    "pgvector index unavailable (%s); the vector channel falls back "
                    "to in-process cosine",
                    type(error).__name__,
                )
                return False
        return True

    def load(self, *, app_name, user_id):
        with self.database.pool.connection() as connection:
            # The revision is read FIRST on purpose. The pool is autocommit, so
            # each statement gets its own snapshot; reading it last would let a
            # concurrent publish hand us revision N with the N-1 corpus, and the
            # next publish would then take a free number and delete the other
            # writer's work. Reading it first can only understate the revision,
            # which makes the next publish collide and conflict correctly.
            revision = connection.execute(
                "SELECT repository_revision, resolutions FROM core_memory_revisions "
                "WHERE app_name = %s AND user_id = %s "
                "ORDER BY repository_revision DESC LIMIT 1",
                (app_name, user_id),
            ).fetchone()
            documents = connection.execute(
                "SELECT memory_id, namespace, path, content, revision, embedding, "
                "entities FROM core_memory_documents "
                "WHERE app_name = %s AND user_id = %s "
                "ORDER BY path",
                (app_name, user_id),
            ).fetchall()
            versions = connection.execute(
                "SELECT memory_id, namespace, path, content, revision "
                "FROM core_memory_document_versions "
                "WHERE app_name = %s AND user_id = %s "
                "ORDER BY memory_id, revision",
                (app_name, user_id),
            ).fetchall()
        return LoadedMemory(
            documents=tuple(self._document(row) for row in documents),
            versions=tuple(self._document(row) for row in versions),
            resolutions=dict((revision or {}).get("resolutions") or {}),
            repository_revision=(revision or {}).get("repository_revision") or 0,
        )

    @staticmethod
    def _document(row):
        embedding = row.get("embedding")
        entities = row.get("entities")
        return StoredDocument(
            row["memory_id"],
            row["namespace"],
            row["path"],
            row["content"],
            row["revision"],
            tuple(embedding) if embedding else None,
            tuple(entities) if isinstance(entities, list) else None,
        )

    def publish(
        self, *, app_name, user_id, repository_revision, documents, resolutions
    ):
        keep = [item.memory_id for item in documents]
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO core_memory_revisions "
                    "(app_name, user_id, repository_revision, resolutions) "
                    "VALUES (%s, %s, %s, %s)",
                    (
                        app_name,
                        user_id,
                        repository_revision,
                        json.dumps(dict(sorted(resolutions.items())), sort_keys=True),
                    ),
                )
                for item in documents:
                    embedding = list(item.embedding) if item.embedding else None
                    entities = (
                        json.dumps(list(item.entities))
                        if item.entities is not None
                        else None
                    )
                    connection.execute(
                        "INSERT INTO core_memory_documents (app_name, user_id, "
                        "memory_id, namespace, path, content, revision, embedding, "
                        "entities) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (app_name, user_id, memory_id) DO UPDATE SET "
                        "namespace = EXCLUDED.namespace, path = EXCLUDED.path, "
                        "content = EXCLUDED.content, revision = EXCLUDED.revision, "
                        "embedding = EXCLUDED.embedding, "
                        "entities = EXCLUDED.entities, updated_at = now()",
                        (
                            app_name,
                            user_id,
                            item.memory_id,
                            item.namespace,
                            item.path,
                            item.content,
                            item.revision,
                            embedding,
                            entities,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO core_memory_document_versions (app_name, "
                        "user_id, memory_id, namespace, path, content, revision) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (app_name, user_id, memory_id, revision) "
                        "DO NOTHING",
                        (
                            app_name,
                            user_id,
                            item.memory_id,
                            item.namespace,
                            item.path,
                            item.content,
                            item.revision,
                        ),
                    )
                connection.execute(
                    "DELETE FROM core_memory_documents WHERE app_name = %s "
                    "AND user_id = %s AND NOT (memory_id = ANY(%s))",
                    (app_name, user_id, keep),
                )
        except errors.UniqueViolation as error:
            raise CoreError(
                "MEMORY_CONFLICT",
                "repository revision already published",
                # The caller needs the revision the store is actually at. Deriving
                # it from the number we just tried only ever returns our own stale
                # base, which is the one value guaranteed to be useless.
                data={"current_revision": self._current_revision(app_name, user_id)},
            ) from error

    def _current_revision(self, app_name, user_id):
        with self.database.pool.connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(max(repository_revision), 0) AS revision "
                "FROM core_memory_revisions WHERE app_name = %s AND user_id = %s",
                (app_name, user_id),
            ).fetchone()
        return row["revision"]

    def vector_candidates(self, *, app_name, user_id, namespace, embedding, limit):
        """Rank by cosine distance in SQL; None means the caller scores in process."""
        if not self.vector_index_available or not embedding:
            return None
        # A type modifier must be a literal in the statement text — PostgreSQL
        # rejects `vector($1)`. int() is what keeps that interpolation safe.
        dimension = int(len(embedding))
        literal = "[" + ",".join(repr(float(value)) for value in embedding) + "]"
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                f"SELECT memory_id, 1 - (embedding::vector({dimension}) <=> "
                f"%s::vector({dimension})) AS similarity "
                "FROM core_memory_documents "
                "WHERE app_name = %s AND user_id = %s AND namespace = %s "
                # A vector of another length is stored but is not in the partial
                # index; feeding it to the operator would raise instead of rank.
                "AND embedding IS NOT NULL AND array_length(embedding, 1) = %s "
                f"ORDER BY embedding::vector({dimension}) <=> "
                f"%s::vector({dimension}) LIMIT %s",
                (literal, app_name, user_id, namespace, dimension, literal, limit),
            ).fetchall()
        return {row["memory_id"]: float(row["similarity"]) for row in rows}

    def close(self):
        return None


def create_memory_store(storage_type, *, database=None, embedding_dimension=768):
    if storage_type == "in-memory":
        return InMemoryMemoryStore()
    if storage_type == "postgres":
        if database is None:
            raise CoreError(
                "CONFIG_INVALID",
                "MEMORY_STORAGE_TYPE=postgres requires a configured database",
            )
        return PostgresMemoryStore(
            database, embedding_dimension=embedding_dimension
        )
    raise CoreError(
        "CONFIG_INVALID", "MEMORY_STORAGE_TYPE must be in-memory or postgres"
    )
