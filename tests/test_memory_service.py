from __future__ import annotations

import json
import os
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from tests.app_support import create_app
from core_agent.audit import InMemoryAuditLog
from core_agent.config import AgentConfig, PlatformConfig, RunRequest
from core_agent.durability import CheckpointStore, InMemoryEventStore
from core_agent.errors import CoreError
from core_agent.execution import ExecutionEnvironmentManager
from core_agent.mcp import InMemoryMcpConnector
from core_agent.memory import MemoryRegistry
from core_agent.memory_providers import HttpEmbeddingProvider, LlmEntityExtractor
from core_agent.memory_store import InMemoryMemoryStore, PostgresMemoryStore
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.observability import RecordingExporter, Telemetry
from core_agent.runtime import CoreAgent
from core_agent.tasks import TaskScheduler
from core_agent.tools import ToolDefinition, ToolRegistry, ToolRuntime

MEMORY_TOOLS = (
    "core_memory_search",
    "core_memory_read",
    "core_memory_create",
    "core_memory_update",
    "core_memory_split",
    "core_memory_delete",
)

NAMESPACE = "subject/user-42"

BASE_ENVIRONMENT = {
    "CORE_AGENT_ENVIRONMENT": "development",
    "NO_PROXY": "*",
    "no_proxy": "*",
    "SESSION_STORAGE_TYPE": "in-memory",
    "LLM_MODEL": "scripted",
    "LLM_API_BASE": "https://model.test/v1",
    "LLM_API_KEY": "sk-not-a-real-key-000000",
}


def body(lines):
    return "\n".join(f"line {index}" for index in range(lines))


class KeywordEmbeddings:
    """Deterministic stand-in for a remote endpoint: one axis per keyword."""

    version = "keyword-v1"

    def __init__(self):
        self.calls = []

    def embed(self, text):
        self.calls.append(text)
        lowered = text.lower()
        return (
            1.0 if "postgres" in lowered else 0.0,
            1.0 if "migration" in lowered else 0.0,
            0.5,
        )


class FailingEmbeddings:
    version = "failing-v1"

    def embed(self, text):
        return None


class UnusedBackend:
    """Memory tools never execute anything; the manager just needs a local one."""

    local = True
    capabilities = {
        "pty",
        "process_groups",
        "workspace_separation",
        "resource_limits",
        "process_tree_teardown",
    }

    def create(self, spec):
        raise AssertionError("memory tools must not touch the execution backend")


def make_service(store=None, **options):
    registry = MemoryRegistry(store or InMemoryMemoryStore(), **options)
    return registry.service("core-agent", "user-42"), registry


class MemoryDomainTests(unittest.TestCase):
    def setUp(self):
        self.service, self.registry = make_service()

    def tearDown(self):
        self.registry.close()

    def test_exactly_two_hundred_body_lines_are_allowed(self):
        document, result = self.service.create(
            title="Long note", body=body(200), namespace=NAMESPACE
        )
        self.assertEqual(document.body_line_count, 200)
        self.assertEqual(result.repository_revision, 1)

    def test_create_over_the_limit_is_rejected_without_any_side_effect(self):
        with self.assertRaises(CoreError) as caught:
            self.service.create(title="Too long", body=body(201), namespace=NAMESPACE)
        self.assertEqual(caught.exception.code, "MEMORY_FILE_TOO_LARGE")
        self.assertEqual(self.service.repository_revision, 0)
        self.assertEqual(self.service.list_documents(), {})

    def test_rejection_reports_actual_and_max_lines_with_a_split_recommendation(self):
        with self.assertRaises(CoreError) as caught:
            self.service.create(
                title="Too long",
                body="# Overview\n" + body(200) + "\n# Decisions",
                namespace=NAMESPACE,
            )
        data = caught.exception.data
        self.assertEqual(data["actual_body_lines"], 202)
        self.assertEqual(data["max_body_lines"], 200)
        self.assertEqual(
            data["recommended_action"], "split_into_multiple_markdown_files"
        )
        self.assertFalse(data["committed"])
        # Boundaries come from the headings the writer already chose.
        self.assertEqual(data["suggested_boundaries"], ["Overview", "Decisions"])

    def test_update_over_the_limit_leaves_the_committed_document_untouched(self):
        document, _ = self.service.create(
            title="Note", body="short", namespace=NAMESPACE
        )
        with self.assertRaises(CoreError):
            self.service.update(document.id, body=body(201), expected_revision=1)
        self.assertEqual(self.service.read(document.id).body, "short")
        self.assertEqual(self.service.repository_revision, 1)

    def test_update_requires_the_revision_that_was_read(self):
        document, _ = self.service.create(
            title="Note", body="first", namespace=NAMESPACE
        )
        self.service.update(document.id, body="second", expected_revision=1)
        with self.assertRaises(CoreError) as caught:
            self.service.update(document.id, body="third", expected_revision=1)
        self.assertEqual(caught.exception.code, "MEMORY_CONFLICT")
        self.assertEqual(caught.exception.data["current_revision"], 2)
        self.assertEqual(self.service.read(document.id).body, "second")

    def test_runtime_composes_front_matter_so_the_model_writes_only_the_body(self):
        document, _ = self.service.create(
            title="Database migration decisions",
            body="Alice chose PostgreSQL.",
            namespace=NAMESPACE,
            kind="decision",
            sources=({"task_id": "task-1", "event_revision": 7},),
        )
        self.assertTrue(document.content.startswith("---\n"))
        self.assertIn(f"id: {document.id}", document.content)
        self.assertIn("namespace: subject/user-42", document.content)
        self.assertIn("kind: decision", document.content)
        self.assertIn("task_id: task-1", document.content)
        self.assertEqual(document.body, "Alice chose PostgreSQL.")

    def test_split_is_atomic_and_an_oversized_child_rejects_the_whole_plan(self):
        document, _ = self.service.create(
            title="Everything", body="all of it", namespace=NAMESPACE
        )
        with self.assertRaises(CoreError) as caught:
            self.service.split(
                document.id,
                overview={"body": "See children."},
                children=[
                    {"title": "Small", "body": "fits"},
                    {"title": "Huge", "body": body(201)},
                ],
                expected_revision=1,
            )
        self.assertEqual(caught.exception.code, "MEMORY_FILE_TOO_LARGE")
        self.assertEqual(len(self.service.list_documents()), 1)
        self.assertEqual(self.service.read(document.id).body, "all of it")

        memory_ids, result = self.service.split(
            document.id,
            overview={"body": "See children."},
            children=[
                {"title": "Engine", "body": "PostgreSQL 17."},
                {"title": "Schema", "body": "Migration 9."},
            ],
            expected_revision=1,
        )
        self.assertEqual(len(memory_ids), 3)
        # The stable id stays with the overview.
        self.assertEqual(memory_ids[0], document.id)
        self.assertEqual(result.repository_revision, 2)
        self.assertEqual(len(self.service.list_documents()), 3)

    def test_delete_removes_the_document_from_reads_and_from_search(self):
        document, _ = self.service.create(
            title="Obsolete", body="Acme picked Redis.", namespace=NAMESPACE
        )
        self.assertTrue(self.service.search("Redis", namespace=NAMESPACE).results)
        self.service.delete(document.id, reason="superseded", expected_revision=1)
        with self.assertRaises(CoreError) as caught:
            self.service.read(document.id)
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.assertEqual(self.service.search("Redis", namespace=NAMESPACE).results, ())
        self.assertEqual(self.service.graph_mentions("Acme"), ())

    def test_session_scope_is_invisible_to_a_search_in_user_scope(self):
        self.service.create(
            title="Session only", body="ephemeral", namespace="session/ctx-1"
        )
        self.service.create(title="User wide", body="durable", namespace=NAMESPACE)
        session_hits = self.service.search("ephemeral", namespace="session/ctx-1")
        user_hits = self.service.search("ephemeral", namespace=NAMESPACE)
        self.assertEqual(len(session_hits.results), 1)
        self.assertEqual(user_hits.results, ())

    def test_committed_state_history_and_embeddings_survive_a_restart(self):
        service, registry = make_service(embedding_provider=KeywordEmbeddings())
        document, _ = service.create(
            title="Migration", body="PostgreSQL migration.", namespace=NAMESPACE
        )
        service.update(document.id, body="PostgreSQL migration 9.", expected_revision=1)

        reopened, reopened_registry = make_service(
            registry.store, embedding_provider=KeywordEmbeddings()
        )
        try:
            self.assertEqual(reopened.repository_revision, 2)
            self.assertEqual(reopened.read(document.id).revision, 2)
            self.assertEqual(reopened.read(document.id).body, "PostgreSQL migration 9.")
            self.assertEqual(
                [item.revision for item in reopened.history(document.id)], [1, 2]
            )
            # The stored vector is reused, not recomputed from the corpus.
            self.assertIn(document.id, reopened._embeddings)
        finally:
            reopened_registry.close()

    def test_embedding_is_computed_per_write_not_per_search(self):
        embeddings = KeywordEmbeddings()
        service, registry = make_service(embedding_provider=embeddings)
        try:
            for index in range(5):
                service.create(
                    title=f"Note {index}",
                    body=f"PostgreSQL detail {index}",
                    namespace=NAMESPACE,
                )
            after_writes = len(embeddings.calls)
            service.search("postgres migration", namespace=NAMESPACE)
            service.search("postgres schema", namespace=NAMESPACE)
        finally:
            registry.close()
        # Five documents, five embeddings; each later search embeds only its query.
        self.assertEqual(after_writes, 5)
        self.assertEqual(len(embeddings.calls), 7)

    def test_search_finds_a_note_written_in_a_non_latin_script(self):
        """A latin-only character class empties both query and document at once."""
        document, _ = self.service.create(
            title="Тест памяти",
            body="Тестирование памяти: создание, поиск и чтение записей.",
            namespace=NAMESPACE,
        )
        response = self.service.search("тест памяти", namespace=NAMESPACE)
        self.assertEqual(
            [item.memory_id for item in response.results], [document.id]
        )
        # The graph channel has to see the entity too, not only BM25.
        self.assertIn(document.id, self.service.graph_mentions("Тестирование"))

    def test_search_mixes_scripts_within_one_corpus(self):
        russian, _ = self.service.create(
            title="Миграция", body="Выбрали PostgreSQL 17.", namespace=NAMESPACE
        )
        english, _ = self.service.create(
            title="Migration", body="Chose PostgreSQL 17.", namespace=NAMESPACE
        )
        by_russian = self.service.search("миграция", namespace=NAMESPACE)
        by_english = self.service.search("migration", namespace=NAMESPACE)
        by_shared = self.service.search("postgresql", namespace=NAMESPACE)
        self.assertEqual([item.memory_id for item in by_russian.results], [russian.id])
        self.assertEqual([item.memory_id for item in by_english.results], [english.id])
        self.assertEqual(
            {item.memory_id for item in by_shared.results}, {russian.id, english.id}
        )

    def test_search_without_an_embedding_layer_degrades_instead_of_failing(self):
        self.service.create(
            title="Note", body="PostgreSQL migration.", namespace=NAMESPACE
        )
        response = self.service.search("migration", namespace=NAMESPACE)
        self.assertTrue(response.results)
        self.assertEqual(
            response.degraded_channels["vector"], "embeddings not configured"
        )
        self.assertIsNone(response.results[0].scores["vector"])

    def test_a_failing_embedding_endpoint_degrades_the_channel_not_the_write(self):
        service, registry = make_service(embedding_provider=FailingEmbeddings())
        try:
            document, result = service.create(
                title="Note", body="PostgreSQL migration.", namespace=NAMESPACE
            )
            self.assertTrue(result.committed)
            response = service.search("migration", namespace=NAMESPACE)
            self.assertTrue(response.results)
            self.assertEqual(response.results[0].memory_id, document.id)
        finally:
            registry.close()


class FrontMatterInjectionTests(unittest.TestCase):
    """Model-supplied values are untrusted input to a line-oriented format."""

    def setUp(self):
        self.service, self.registry = make_service()

    def tearDown(self):
        self.registry.close()

    def test_a_newline_in_the_title_cannot_forge_another_documents_id(self):
        victim, _ = self.service.create(
            title="Victim", body="original body", namespace=NAMESPACE
        )
        with self.assertRaises(CoreError) as caught:
            self.service.create(
                title=f"Innocent\nid: {victim.id}",
                body="attacker body",
                namespace=NAMESPACE,
            )
        self.assertEqual(caught.exception.code, "MEMORY_INVALID")
        # The victim survives untouched, at its original revision.
        self.assertEqual(self.service.read(victim.id).body, "original body")
        self.assertEqual(self.service.read(victim.id).revision, 1)
        self.assertEqual(len(self.service.list_documents()), 1)

    def test_a_newline_in_the_kind_cannot_forge_the_namespace(self):
        with self.assertRaises(CoreError) as caught:
            self.service.create(
                title="Note",
                body="planted",
                namespace=NAMESPACE,
                kind="fact\nnamespace: session/someone-elses-session",
            )
        self.assertEqual(caught.exception.code, "MEMORY_INVALID")
        self.assertEqual(
            self.service.search("planted", namespace="session/someone-elses-session")
            .results,
            (),
        )

    def test_every_model_supplied_front_matter_value_is_checked(self):
        for field, arguments in (
            ("title", {"title": "a\nstatus: archived"}),
            ("kind", {"kind": "fact\nstatus: archived"}),
            ("tags", {"tags": ("ok", "bad\nstatus: archived")}),
        ):
            with self.subTest(field=field):
                with self.assertRaises(CoreError) as caught:
                    self.service.create(
                        title=arguments.pop("title", "Note"),
                        body="body",
                        namespace=NAMESPACE,
                        **arguments,
                    )
                self.assertEqual(caught.exception.code, "MEMORY_INVALID")

    def test_the_guard_uses_the_parsers_own_definition_of_a_line_break(self):
        """U+2028, U+2029 and NEL split a line for splitlines() but not for ord()."""
        victim, _ = self.service.create(
            title="Victim",
            body="ok",
            namespace=NAMESPACE,
            tags=("alpha",),
            sources=({"task_id": "task-1", "event_revision": 42},),
        )
        for name, separator in (
            ("line separator", " "),
            ("paragraph separator", " "),
            ("next line", "\x85"),
        ):
            with self.subTest(separator=name):
                payload = (
                    f"Innocent{separator}kind: hijacked{separator}"
                    f"status: superseded{separator}sources:{separator}---"
                )
                with self.assertRaises(CoreError) as caught:
                    self.service.update(
                        victim.id, body="x", expected_revision=1, title=payload
                    )
                self.assertEqual(caught.exception.code, "MEMORY_INVALID")
        # The immutable fields and the provenance are untouched.
        current = self.service.read(victim.id)
        self.assertEqual(current.kind, "fact")
        self.assertEqual(current.status, "active")
        self.assertIn("tags: [alpha]", current.content)
        self.assertIn("task_id: task-1", current.content)

    def test_a_split_child_title_cannot_forge_a_namespace(self):
        document, _ = self.service.create(
            title="Parent", body="body", namespace=NAMESPACE
        )
        with self.assertRaises(CoreError) as caught:
            self.service.split(
                document.id,
                overview={"body": "see children"},
                children=[
                    {
                        "title": "Child namespace: session/victim sources: ---",
                        "body": "planted",
                    }
                ],
                expected_revision=1,
            )
        self.assertEqual(caught.exception.code, "MEMORY_INVALID")
        self.assertEqual(
            self.service.search("planted", namespace="session/victim").results, ()
        )
        self.assertEqual(len(self.service.list_documents()), 1)

    def test_a_tag_that_cannot_survive_the_round_trip_is_refused(self):
        for tag in ("Q1, 2026", "[a]", "]"):
            with self.subTest(tag=tag):
                with self.assertRaises(CoreError) as caught:
                    self.service.create(
                        title="Note", body="b", namespace=NAMESPACE, tags=(tag,)
                    )
                self.assertEqual(caught.exception.code, "MEMORY_INVALID")

    def test_one_unreadable_stored_row_does_not_take_the_corpus_with_it(self):
        """A row written before a rule existed must cost only itself."""
        from core_agent.memory_store import LoadedMemory, StoredDocument

        good = (
            "---\nid: mem_good\ntitle: Good\nnamespace: subject/user-42\n"
            "kind: fact\nstatus: active\ncreated_at: x\nupdated_at: y\n"
            "tags: []\nsources:\n---\nfine"
        )

        class PoisonedStore(InMemoryMemoryStore):
            def load(self, *, app_name, user_id):
                return LoadedMemory(
                    documents=(StoredDocument("mem_good", NAMESPACE, "g.md", good, 1),),
                    versions=(
                        StoredDocument(
                            "mem_bad",
                            NAMESPACE,
                            "b.md",
                            good.replace("sources:", "id: mem_dup\nsources:"),
                            1,
                        ),
                    ),
                    repository_revision=1,
                )

        service, registry = make_service(PoisonedStore())
        try:
            self.assertEqual(list(service.list_documents()), ["mem_good"])
            self.assertTrue(service.search("fine", namespace=NAMESPACE).results)
            self.assertEqual(
                [item["memory_id"] for item in service._unreadable], ["mem_bad"]
            )
        finally:
            registry.close()

    def test_a_duplicate_front_matter_key_is_refused_by_the_parser(self):
        """Second guard on the same boundary, independent of the composer."""
        forged = (
            "---\nid: mem_one\ntitle: T\nnamespace: subject/user-42\nkind: fact\n"
            "status: active\ncreated_at: x\nupdated_at: y\nid: mem_two\nsources:\n"
            "---\nbody"
        )
        with self.assertRaises(CoreError) as caught:
            self.service._parse("p.md", forged, 1)
        self.assertEqual(caught.exception.code, "MEMORY_INVALID")

    def test_update_and_split_keep_tags_and_sources(self):
        document, _ = self.service.create(
            title="Decision",
            body="first",
            namespace=NAMESPACE,
            tags=("database", "migration"),
            sources=({"task_id": "task-1", "event_revision": 42},),
        )
        updated, _ = self.service.update(document.id, body="second", expected_revision=1)
        self.assertIn("tags: [database, migration]", updated.content)
        self.assertIn("task_id: task-1", updated.content)
        self.assertIn("event_revision: 42", updated.content)

        memory_ids, _ = self.service.split(
            document.id,
            overview={"body": "see children"},
            children=[{"title": "Child", "body": "detail"}],
            expected_revision=2,
        )
        overview = self.service.read(memory_ids[0])
        self.assertIn("tags: [database, migration]", overview.content)
        self.assertIn("task_id: task-1", overview.content)


class ExtractionAndDegradationTests(unittest.TestCase):
    def test_extraction_only_reruns_for_documents_whose_content_changed(self):
        """Re-extracting the corpus per write is N remote calls per tool call."""

        class CountingExtractor:
            version = "counting-ner-v1"

            def __init__(self):
                self.calls = 0

            def extract(self, text):
                self.calls += 1
                return {"entities": [], "relations": []}

        extractor = CountingExtractor()
        service, registry = make_service(entity_extractor=extractor)
        try:
            for index in range(20):
                service.create(
                    title=f"Note {index}", body=f"body {index}", namespace=NAMESPACE
                )
            after_twenty = extractor.calls
            service.create(title="Note 20", body="body 20", namespace=NAMESPACE)
        finally:
            registry.close()
        # One extraction per created document, not one per document per write.
        self.assertEqual(after_twenty, 20)
        self.assertEqual(extractor.calls - after_twenty, 1)

    def test_an_extractor_outage_at_load_degrades_search_instead_of_failing_it(self):
        class BrokenExtractor:
            version = "broken-ner-v1"

            def extract(self, text):
                raise RuntimeError("ner unreachable")

        seeded, registry = make_service()
        seeded.create(title="Note", body="migration detail", namespace=NAMESPACE)
        store = registry.store

        reopened, reopened_registry = make_service(
            store, entity_extractor=BrokenExtractor()
        )
        try:
            response = reopened.search("migration", namespace=NAMESPACE)
        finally:
            reopened_registry.close()
        self.assertTrue(response.results)
        self.assertIn("graph", response.degraded_channels)

    def test_an_extraction_outage_during_a_write_keeps_the_note(self):
        """BM25 does not depend on extraction, so losing the note costs more."""

        class BrokenExtractor:
            version = "broken-ner-v1"

            def extract(self, text):
                raise RuntimeError("model unreachable")

        service, registry = make_service(entity_extractor=BrokenExtractor())
        try:
            with self.assertLogs("core_agent.runtime", "WARNING"):
                document, result = service.create(
                    title="Note", body="migration detail", namespace=NAMESPACE
                )
            self.assertTrue(result.committed)
            self.assertEqual(service.repository_revision, 1)
            response = service.search("migration", namespace=NAMESPACE)
            self.assertEqual(
                [item.memory_id for item in response.results], [document.id]
            )
            # A count, never which notes: the list itself is memory content.
            self.assertEqual(response.degraded_channels["graph"], "1 document(s) not extracted")
            stored = registry.store.load(app_name="core-agent", user_id="user-42")
            self.assertIsNone(stored.documents[0].entities)
        finally:
            registry.close()

    def test_stored_entities_survive_a_restart_without_asking_the_model(self):
        """One model call per note per cold start is what persistence buys off."""

        class CountingExtractor:
            version = "counting-ner-v1"

            def __init__(self):
                self.calls = 0

            def extract(self, text):
                self.calls += 1
                return {
                    "entities": [
                        {
                            "text": "Cloud.ru ML Inference",
                            "type": "product",
                            "start": 0,
                            "end": 21,
                            "confidence": 0.9,
                        }
                    ],
                    "relations": [],
                }

        seeded, registry = make_service(entity_extractor=CountingExtractor())
        seeded.create(
            title="Deploy", body="Cloud.ru ML Inference hosts it", namespace=NAMESPACE
        )
        store = registry.store

        reopened_extractor = CountingExtractor()
        reopened, reopened_registry = make_service(
            store, entity_extractor=reopened_extractor
        )
        try:
            self.assertEqual(reopened_extractor.calls, 0)
            response = reopened.search("Inference", namespace=NAMESPACE)
            self.assertNotIn("graph", response.degraded_channels)
            # A multiword entity is matched by one word of the query; comparing
            # the whole string would silence the channel.
            self.assertGreater(response.results[0].scores["graph"], 0)
            self.assertEqual(
                response.results[0].graph_paths,
                (f"query->Cloud.ru ML Inference->{response.results[0].memory_id}",),
            )
        finally:
            reopened_registry.close()

    def test_search_span_names_the_degraded_channels(self):
        telemetry = Telemetry(RecordingExporter())
        service, registry = make_service(telemetry=telemetry)
        try:
            service.create(title="Note", body="migration", namespace=NAMESPACE)
            service.search("migration", namespace=NAMESPACE)
        finally:
            registry.close()
        spans = {span.name: span for span in telemetry.exporter.spans}
        self.assertIn("core_agent.memory.ner", spans)
        self.assertIn("core_agent.memory.search.vector", spans)
        degraded = spans["core_agent.memory.search"].attributes[
            "core_agent.memory.degraded_channels"
        ]
        self.assertIn("vector", degraded)
        self.assertIn("graph", degraded)

    def test_the_span_records_degradation_discovered_during_the_search(self):
        """A configured endpoint failing at query time is the case that matters."""

        class DyingEmbeddings:
            version = "dying-v1"

            def __init__(self):
                self.calls = 0

            def embed(self, text):
                self.calls += 1
                return (1.0, 0.0) if self.calls == 1 else None

        telemetry = Telemetry(RecordingExporter())
        service, registry = make_service(
            embedding_provider=DyingEmbeddings(), telemetry=telemetry
        )
        try:
            service.create(title="Note", body="migration", namespace=NAMESPACE)
            response = service.search("migration", namespace=NAMESPACE)
        finally:
            registry.close()
        span = next(
            item
            for item in telemetry.exporter.spans
            if item.name == "core_agent.memory.search"
        )
        recorded = span.attributes["core_agent.memory.degraded_channels"]
        self.assertIn("vector", recorded)
        self.assertIn("vector", response.degraded_channels)
        self.assertEqual(sorted(response.degraded_channels), sorted(recorded.split(",")))


class EmbeddingProviderTests(unittest.TestCase):
    def test_endpoint_failure_returns_no_vector_and_leaks_no_input_or_key(self):
        provider = HttpEmbeddingProvider(
            "https://embed.test/v1",
            "hosted_vllm/bge-m3",
            api_key="sk-secret-key-value",
            dimension=3,
        )
        self.assertEqual(provider.model, "bge-m3")
        self.assertEqual(provider.endpoint, "https://embed.test/v1/embeddings")
        with self.assertLogs("core_agent.runtime", "WARNING") as captured:
            with patch(
                "core_agent.memory_providers.urlopen", side_effect=TimeoutError()
            ):
                vector = provider.embed("a confidential memory body")
        self.assertIsNone(vector)
        encoded = "\n".join(captured.output)
        self.assertNotIn("sk-secret-key-value", encoded)
        self.assertNotIn("confidential", encoded)

    def test_a_truncated_body_or_a_malformed_base_url_yields_no_vector(self):
        """The spec says any embedding failure degrades; any means any."""
        from http.client import IncompleteRead

        provider = HttpEmbeddingProvider("https://embed.test/v1", "m", dimension=2)
        with self.assertLogs("core_agent.runtime", "WARNING"):
            with patch(
                "core_agent.memory_providers.urlopen",
                side_effect=IncompleteRead(b"partial"),
            ):
                self.assertIsNone(provider.embed("text"))

        broken = HttpEmbeddingProvider("embed.test/v1", "m", dimension=2)
        with self.assertLogs("core_agent.runtime", "WARNING"):
            self.assertIsNone(broken.embed("text"))

    def test_text_is_truncated_before_it_reaches_the_endpoint(self):
        provider = HttpEmbeddingProvider("https://embed.test/v1", "m", dimension=2)
        sent = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return json.dumps({"data": [{"embedding": [3.0, 4.0]}]}).encode()[:limit]

        def capture(request, timeout=None):
            sent["input"] = json.loads(request.data)["input"]
            return Response()

        with patch("core_agent.memory_providers.urlopen", capture):
            vector = provider.embed("x" * 12_000)
        self.assertEqual(len(sent["input"]), 8000)
        self.assertEqual(vector, (0.6, 0.8))


class LlmEntityExtractorTests(unittest.TestCase):
    @staticmethod
    def _gateway(payload, captured=None):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                body = {"choices": [{"message": {"content": json.dumps(payload)}}]}
                return json.dumps(body).encode()[:limit]

        def respond(request, timeout=None):
            if captured is not None:
                captured.update(json.loads(request.data))
            return Response()

        return respond

    def test_the_whole_schema_travels_with_every_request(self):
        """A schema named by reference would depend on gateway state we cannot see."""
        extractor = LlmEntityExtractor(
            "https://gateway.test/v1/", "Qwen/Qwen3", api_key="sk-secret-key-value"
        )
        self.assertEqual(
            extractor.endpoint, "https://gateway.test/v1/chat/completions"
        )
        sent = {}
        with patch(
            "core_agent.memory_providers.urlopen",
            self._gateway({"entities": []}, sent),
        ):
            self.assertEqual(extractor.extract("nothing here"), {
                "entities": [],
                "relations": [],
            })
        schema = sent["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        self.assertEqual(
            sorted(schema["schema"]["properties"]["entities"]["items"]["required"]),
            ["confidence", "text", "type"],
        )
        self.assertEqual(sent["temperature"], 0)

    def test_an_invented_entity_is_dropped_and_offsets_come_from_the_note(self):
        extractor = LlmEntityExtractor("https://gateway.test/v1", "m")
        text = "Эмиль развернул Cloud.ru ML Inference"
        payload = {
            "entities": [
                {"text": "эмиль", "type": "person", "confidence": 0.9},
                {"text": "Cloud.ru ML Inference", "type": "product", "confidence": 0.8},
                {"text": "Google Cloud", "type": "organization", "confidence": 0.99},
                {"text": "Эмиль", "type": "person", "confidence": 0.7},
            ]
        }
        with patch(
            "core_agent.memory_providers.urlopen", self._gateway(payload)
        ):
            entities = extractor.extract(text)["entities"]
        # The note's own spelling, the note's own offsets, and nothing the note
        # does not contain.
        self.assertEqual(
            [(item["text"], item["start"], item["end"]) for item in entities],
            [("Эмиль", 0, 5), ("Cloud.ru ML Inference", 16, 37)],
        )
        self.assertEqual(text[0:5], "Эмиль")

    def test_an_unusable_type_or_confidence_does_not_lose_the_entity(self):
        extractor = LlmEntityExtractor("https://gateway.test/v1", "m")
        payload = {
            "entities": [{"text": "Redis", "type": "database", "confidence": 42}]
        }
        with patch("core_agent.memory_providers.urlopen", self._gateway(payload)):
            entities = extractor.extract("Redis is used here")["entities"]
        self.assertEqual(entities[0]["type"], "other")
        self.assertEqual(entities[0]["confidence"], 0.5)

    def test_a_permanent_rejection_stops_the_next_request(self):
        """A 400 on response_format will not become a 200 without a redeploy."""
        extractor = LlmEntityExtractor(
            "https://gateway.test/v1", "m", api_key="sk-secret-key-value"
        )
        calls = []

        def reject(request, timeout=None):
            calls.append(request)
            raise HTTPError("https://gateway.test", 400, "Bad Request", {}, None)

        with self.assertLogs("core_agent.runtime", "WARNING") as captured:
            with patch("core_agent.memory_providers.urlopen", reject):
                with self.assertRaises(CoreError):
                    extractor.extract("Эмиль")
                with self.assertRaises(CoreError) as second:
                    extractor.extract("Эмиль")
        self.assertEqual(len(calls), 1)
        self.assertEqual(second.exception.code, "MEMORY_PROVIDER_UNAVAILABLE")
        encoded = "\n".join(captured.output)
        self.assertNotIn("sk-secret-key-value", encoded)
        self.assertNotIn("Эмиль", encoded)

    def test_a_rate_limit_or_timeout_stays_retryable(self):
        extractor = LlmEntityExtractor("https://gateway.test/v1", "m")
        calls = []

        def reject(request, timeout=None):
            calls.append(request)
            raise HTTPError("https://gateway.test", 429, "Too Many", {}, None)

        with patch("core_agent.memory_providers.urlopen", reject):
            for _ in range(2):
                with self.assertRaises(CoreError):
                    extractor.extract("text")
        self.assertEqual(len(calls), 2)
        self.assertIsNone(extractor.disabled_reason)

    def test_a_response_that_is_not_the_promised_shape_is_rejected(self):
        """`strict` is a property of the gateway, not a guarantee to the caller."""
        extractor = LlmEntityExtractor("https://gateway.test/v1", "m")
        for payload in ("not json at all", {"entities": "many"}):
            with self.subTest(payload=payload):
                with patch(
                    "core_agent.memory_providers.urlopen", self._gateway(payload)
                ):
                    with self.assertRaises(CoreError) as caught:
                        extractor.extract("text")
                self.assertEqual(
                    caught.exception.code, "MEMORY_PROVIDER_INVALID_RESPONSE"
                )


def memory_agent(model, *, memory_registry, allow=MEMORY_TOOLS, memory="optional"):
    registry = ToolRegistry()
    for name in MEMORY_TOOLS:
        registry.register(
            ToolDefinition(
                name,
                name,
                {"type": "object", "additionalProperties": True},
                mutating=False,
                risk_tags=frozenset(),
            )
        )
    platform = PlatformConfig(
        allowed_builtin_tools=set(MEMORY_TOOLS),
        denied_builtin_tools=set(),
        allowed_mcp_servers=set(),
        denied_mcp_tools={},
        allowed_skills=set(),
        supported_features={"memory", "mcp"},
        max_model_turns=10,
        max_tool_calls=10,
    )
    config = AgentConfig.from_dict(
        {
            "schema_version": "v1alpha1",
            "agent": {"name": "memory-test", "profile_prompt": "Remember things."},
            "model": {"route": "scripted"},
            "features": {
                "memory": memory,
                "background_tasks": False,
                "delegation": False,
                "terminal": False,
                "filesystem_mutation": False,
                "mcp": True,
                "skills": False,
                "human_input": False,
            },
            "tools": {
                "builtins": {"default": "deny", "allow": list(allow), "deny": []},
                "mcp": {"default": "deny", "allow_servers": [], "allow_tools": {}},
            },
            "skills": {"default": "deny", "allow": []},
            "context": {
                "compact_at_working_ratio": 0.90,
                "compact_to_working_ratio": 0.15,
            },
            "execution": {
                "environment_profile": "local-pty-test",
                "runtime_mode": "without_terminal",
            },
            "observability": {"otel_profile": "test"},
            "budgets": {"model_turns": 10, "tool_calls": 10},
        }
    )
    return CoreAgent(
        platform_config=platform,
        agent_config=config,
        model=model,
        tool_runtime=ToolRuntime(registry, ExecutionEnvironmentManager(UnusedBackend())),
        mcp_connector=InMemoryMcpConnector(),
        task_scheduler=TaskScheduler(),
        event_store=InMemoryEventStore(),
        checkpoint_store=CheckpointStore(),
        audit_log=InMemoryAuditLog(),
        telemetry=Telemetry(RecordingExporter()),
        memory_registry=memory_registry,
    )


class MemoryToolTests(unittest.TestCase):
    def setUp(self):
        self.registry = MemoryRegistry(InMemoryMemoryStore())

    def tearDown(self):
        self.registry.close()

    def test_oversized_write_reaches_the_model_and_the_run_survives_to_split(self):
        """The 200-line protocol only works if the model can read the refusal."""
        seen = []

        class SplittingModel:
            model = "scripted"

            def generate(self, *, context, tools, instructions, messages=None):
                seen.append(tuple(messages or ()))
                if len(seen) == 1:
                    return ModelResponse(
                        tool_requests=(
                            ToolRequest(
                                "call-1",
                                "core_memory_create",
                                {"title": "Everything", "body": body(201)},
                            ),
                        ),
                        finish_reason="tool_calls",
                    )
                return ModelResponse(message="split instead", finish_reason="stop")

        agent = memory_agent(SplittingModel(), memory_registry=self.registry)
        try:
            result = agent.run(
                RunRequest.from_dict({"prompt": "Remember everything"}),
                identity="user-42",
                session_id="ctx-1",
            )
        finally:
            agent.close()
        self.assertEqual(result.message, "split instead")
        # Turn two must actually carry the refusal, with the actionable payload.
        delivered = "".join(
            item.get("content", "") for item in seen[1] if isinstance(item, dict)
        )
        self.assertIn("MEMORY_FILE_TOO_LARGE", delivered)
        self.assertIn("actual_body_lines", delivered)
        self.assertIn("split_into_multiple_markdown_files", delivered)

    def test_scope_argument_selects_a_namespace_it_cannot_forge(self):
        agent = memory_agent(
            ScriptedModel([ModelResponse(message="ok")]), memory_registry=self.registry
        )
        try:
            agent._run_scopes["run-1"] = {
                "identity": "user-42",
                "session_id": "ctx-1",
            }
            agent._memory_create(
                {"title": "User note", "body": "durable", "scope": "user"}, "run-1"
            )
            agent._memory_create(
                {"title": "Session note", "body": "ephemeral", "scope": "session"},
                "run-1",
            )
            service = self.registry.service("memory-test", "user-42")
            namespaces = sorted(
                document.namespace for document in service.list_documents().values()
            )
            self.assertEqual(namespaces, ["session/ctx-1", "subject/user-42"])
            # A path smuggled through scope is rejected, not resolved.
            with self.assertRaises(CoreError) as caught:
                agent._memory_search(
                    {"query": "x", "scope": "subject/someone-else"}, "run-1"
                )
            self.assertEqual(caught.exception.code, "TOOL_ARGUMENT_INVALID")
        finally:
            agent.close()

    def test_session_scope_without_a_session_is_a_recoverable_argument_error(self):
        agent = memory_agent(
            ScriptedModel([ModelResponse(message="ok")]), memory_registry=self.registry
        )
        try:
            agent._run_scopes["run-1"] = {"identity": "user-42"}
            with self.assertRaises(CoreError) as caught:
                agent._memory_create(
                    {"title": "t", "body": "b", "scope": "session"}, "run-1"
                )
        finally:
            agent.close()
        self.assertEqual(caught.exception.code, "TOOL_ARGUMENT_INVALID")

    def test_two_users_never_see_each_others_memory(self):
        agent = memory_agent(
            ScriptedModel([ModelResponse(message="ok")]), memory_registry=self.registry
        )
        try:
            agent._run_scopes["run-a"] = {"identity": "user-a"}
            agent._run_scopes["run-b"] = {"identity": "user-b"}
            agent._memory_create({"title": "A", "body": "alpha secret"}, "run-a")
            found = agent._memory_search({"query": "alpha"}, "run-b")
        finally:
            agent.close()
        self.assertEqual(found["results"], [])

    def test_search_returns_the_revision_that_update_will_require(self):
        agent = memory_agent(
            ScriptedModel([ModelResponse(message="ok")]), memory_registry=self.registry
        )
        try:
            agent._run_scopes["run-1"] = {"identity": "user-42"}
            created = agent._memory_create(
                {"title": "Migration", "body": "PostgreSQL 17."}, "run-1"
            )
            found = agent._memory_search({"query": "postgresql"}, "run-1")
            self.assertEqual(found["results"][0]["memory_id"], created["memory_id"])
            self.assertEqual(found["results"][0]["revision"], created["revision"])
            updated = agent._memory_update(
                {
                    "memory_id": created["memory_id"],
                    "body": "PostgreSQL 18.",
                    "expected_revision": found["results"][0]["revision"],
                },
                "run-1",
            )
            self.assertEqual(updated["revision"], 2)
        finally:
            agent.close()

    def test_a_run_without_the_subsystem_reports_a_disabled_capability(self):
        agent = memory_agent(
            ScriptedModel([ModelResponse(message="ok")]), memory_registry=None
        )
        try:
            agent._run_scopes["run-1"] = {"identity": "user-42"}
            with self.assertRaises(CoreError) as caught:
                agent._memory_search({"query": "x"}, "run-1")
        finally:
            agent.close()
        self.assertEqual(caught.exception.code, "CAPABILITY_DISABLED")

    def test_a_delegated_memory_tool_reaches_the_parent_corpus(self):
        agent = memory_agent(
            ScriptedModel([ModelResponse(message="ok")]), memory_registry=self.registry
        )
        try:
            child = agent._child_agent(
                agent.agent_config.to_dict(), ["core_memory_search"]
            )
            self.assertIs(child.memory_registry, agent.memory_registry)
            agent._run_scopes["run-1"] = {"identity": "user-42"}
            agent._memory_create({"title": "Shared", "body": "parent fact"}, "run-1")
            child._run_scopes["run-2"] = {"identity": "user-42"}
            found = child._memory_search({"query": "parent"}, "run-2")
            self.assertEqual(len(found["results"]), 1)
        finally:
            agent.close()


class MemoryConfigurationTests(unittest.TestCase):
    @staticmethod
    def _app(**environment):
        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "scripted"
        with patch.dict(os.environ, {**BASE_ENVIRONMENT, **environment}, clear=True):
            return create_app(model=model)

    def test_memory_tools_are_available_by_default(self):
        app = self._app()
        try:
            catalog = app.state.core_agent.tool_runtime.registry.names()
            allowed = app.state.core_agent.agent_config.to_dict()["tools"]["builtins"][
                "allow"
            ]
        finally:
            app.state.close()
        self.assertTrue(set(MEMORY_TOOLS) <= set(catalog))
        self.assertTrue(set(MEMORY_TOOLS) <= set(allowed))

    def test_extraction_is_wired_from_the_model_gateway_alone(self):
        """No second endpoint and no second credential to get the graph channel."""
        app = self._app()
        try:
            extractor = app.state.core_agent.memory_registry.entity_extractor
        finally:
            app.state.close()
        self.assertEqual(
            extractor.endpoint, "https://model.test/v1/chat/completions"
        )
        self.assertEqual(extractor.model, "scripted")

        # A full endpoint override means the deployment chose the path.
        overridden = self._app(LLM_ENDPOINT="https://gw.test/openai/v1/completions")
        try:
            self.assertEqual(
                overridden.state.core_agent.memory_registry.entity_extractor.endpoint,
                "https://gw.test/openai/v1/completions",
            )
        finally:
            overridden.state.close()

        for absent in ({"LLM_API_KEY": ""}, {"LLM_API_FORMAT": "anthropic"}):
            with self.subTest(absent=absent):
                app = self._app(**absent)
                try:
                    registry = app.state.core_agent.memory_registry
                    self.assertIsNone(registry.entity_extractor)
                finally:
                    app.state.close()

    def test_the_disabled_string_removes_every_memory_tool_from_the_catalog(self):
        """`disabled` is a non-empty string; a truthiness gate would pass it."""
        app = self._app(CORE_AGENT_MEMORY="disabled")
        try:
            allowed = app.state.core_agent.agent_config.to_dict()["tools"]["builtins"][
                "allow"
            ]
            effective = app.state.core_agent._resolve_capabilities(
                RunRequest.from_dict({"prompt": "x"})
            )[2]
            self.assertIsNone(app.state.core_agent.memory_registry)
        finally:
            app.state.close()
        self.assertFalse([name for name in allowed if name.startswith("core_memory_")])
        self.assertFalse(
            [
                name
                for name in effective.builtin_tools
                if name.startswith("core_memory_")
            ]
        )
        self.assertNotIn("memory", effective.enabled_capability_policies)

    def test_production_refuses_memory_that_disappears_on_restart(self):
        from core_agent.app import _memory_registry

        with patch.dict(
            os.environ,
            {
                **BASE_ENVIRONMENT,
                "CORE_AGENT_ENVIRONMENT": "production",
                "MEMORY_STORAGE_TYPE": "in-memory",
            },
            clear=True,
        ):
            with self.assertRaises(CoreError) as caught:
                _memory_registry(None, None, enabled=True)
            # Disabled memory has nothing to lose, so the guard must not fire.
            registry, configuration = _memory_registry(None, None, enabled=False)
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")
        self.assertIn("MEMORY_STORAGE_TYPE", caught.exception.message)
        self.assertIsNone(registry)
        self.assertEqual(configuration["backend"], "disabled")

    def test_durable_memory_works_beside_ephemeral_sessions(self):
        """A configured DATABASE_URL means memory, even with in-memory sessions."""
        from core_agent.app import _memory_registry

        url = os.getenv("TEST_DATABASE_URL")
        if not url:
            self.skipTest("set TEST_DATABASE_URL to open a real pool")
        state = {"database": None, "owned_databases": []}
        with patch.dict(
            os.environ,
            {**BASE_ENVIRONMENT, "MEMORY_STORAGE_TYPE": "postgres", "DATABASE_URL": url},
            clear=True,
        ):
            registry, configuration = _memory_registry(state, None, enabled=True)
        try:
            self.assertEqual(configuration["backend"], "postgres")
            # The pool has no other owner, so shutdown has to close it.
            self.assertEqual(len(state["owned_databases"]), 1)
        finally:
            for owned in state["owned_databases"]:
                owned.close()

    def test_postgres_memory_without_any_dsn_names_both_variables(self):
        from core_agent.app import _memory_registry

        with patch.dict(
            os.environ,
            {**BASE_ENVIRONMENT, "MEMORY_STORAGE_TYPE": "postgres"},
            clear=True,
        ):
            with self.assertRaises(CoreError) as caught:
                _memory_registry(
                    {"database": None, "owned_databases": []}, None, enabled=True
                )
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")
        self.assertIn("DATABASE_URL", caught.exception.message)
        self.assertIn("MEMORY_POSTGRES_HOST", caught.exception.message)

    def test_embedding_layer_needs_all_three_variables(self):
        app = self._app(
            EMBEDDING_MODEL="bge-m3", EMBEDDING_API_BASE="https://e.test/v1"
        )
        try:
            self.assertIsNone(app.state.core_agent.memory_registry.embedding_provider)
        finally:
            app.state.close()
        app = self._app(
            EMBEDDING_MODEL="bge-m3",
            EMBEDDING_API_BASE="https://e.test/v1",
            EMBEDDING_API_KEY="sk-embed",
        )
        try:
            provider = app.state.core_agent.memory_registry.embedding_provider
            self.assertIsNotNone(provider)
            self.assertEqual(provider.headers["X-Internal-Title"], "evo_ai_agents")
        finally:
            app.state.close()

    def test_startup_record_reports_the_memory_backend(self):
        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "scripted"
        with patch.dict(os.environ, BASE_ENVIRONMENT, clear=True):
            with self.assertLogs("core_agent.runtime", "INFO") as captured:
                app = create_app(model=model)
        app.state.close()
        record = next(
            json.loads(item.split(":", 2)[2])
            for item in captured.output
            if '"startup.configuration"' in item
        )
        self.assertEqual(record["memory"]["backend"], "in-memory")
        self.assertFalse(record["memory"]["embeddings"])

    def test_only_six_memory_tools_are_published_to_the_model(self):
        """move, history, index_status and entity_resolve stay internal."""
        app = self._app()
        try:
            published = {
                name
                for name in app.state.core_agent.tool_runtime.registry.names()
                if name.startswith("core_memory_")
            }
        finally:
            app.state.close()
        self.assertEqual(published, set(MEMORY_TOOLS))

    def test_internal_operations_remain_available_to_the_subsystem(self):
        service, registry = make_service()
        try:
            document, _ = service.create(
                title="Note", body="Alice ships.", namespace=NAMESPACE
            )
            self.assertEqual(len(service.history(document.id)), 1)
            self.assertEqual(
                set(service.index_status()),
                {"markdown", "chunks", "bm25", "vectors", "ner", "graph"},
            )
            service.entity_resolve("entity:Alice", "entity:alice-1")
            self.assertEqual(service.resolve_entity("Alice"), "entity:alice-1")
        finally:
            registry.close()


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL"),
    "set TEST_DATABASE_URL to run PostgreSQL memory tests",
)
class PostgresMemoryTests(unittest.TestCase):
    def setUp(self):
        from core_agent.database import PostgresDatabase

        self.database = PostgresDatabase(
            os.environ["TEST_DATABASE_URL"], min_size=0, max_size=4
        )
        with self.database.transaction() as connection:
            connection.execute(
                "TRUNCATE core_memory_documents, core_memory_document_versions, "
                "core_memory_revisions"
            )
        self.store = PostgresMemoryStore(self.database, embedding_dimension=3)

    def tearDown(self):
        self.database.close()

    def _service(self, embeddings=None, extractor=None):
        registry = MemoryRegistry(
            self.store, embedding_provider=embeddings, entity_extractor=extractor
        )
        return registry.service("core-agent", "user-42")

    def test_extracted_entities_are_a_column_and_not_a_second_extraction(self):
        class OnceExtractor:
            version = "once-ner-v1"

            def __init__(self):
                self.calls = 0

            def extract(self, text):
                self.calls += 1
                return {
                    "entities": [
                        {
                            "text": "PostgreSQL",
                            "type": "technology",
                            "start": 0,
                            "end": 10,
                            "confidence": 0.9,
                        }
                    ],
                    "relations": [],
                }

        service = self._service(extractor=OnceExtractor())
        document, _ = service.create(
            title="Engine", body="PostgreSQL is the store.", namespace=NAMESPACE
        )
        with self.database.pool.connection() as connection:
            row = connection.execute(
                "SELECT entities FROM core_memory_documents WHERE memory_id = %s",
                (document.id,),
            ).fetchone()
        self.assertEqual(row["entities"][0]["text"], "PostgreSQL")

        reopened_extractor = OnceExtractor()
        reopened = self._service(extractor=reopened_extractor)
        # A cold start on a scale-to-zero platform must not be one model call
        # per note.
        self.assertEqual(reopened_extractor.calls, 0)
        response = reopened.search("PostgreSQL", namespace=NAMESPACE)
        self.assertNotIn("graph", response.degraded_channels)
        self.assertGreater(response.results[0].scores["graph"], 0)

    def test_publication_survives_restart_with_versions_and_embeddings(self):
        service = self._service(KeywordEmbeddings())
        document, _ = service.create(
            title="Migration", body="PostgreSQL migration.", namespace=NAMESPACE
        )
        service.update(document.id, body="PostgreSQL migration 9.", expected_revision=1)

        reopened = self._service(KeywordEmbeddings())
        self.assertEqual(reopened.repository_revision, 2)
        self.assertEqual(reopened.read(document.id).body, "PostgreSQL migration 9.")
        self.assertEqual(
            [item.revision for item in reopened.history(document.id)], [1, 2]
        )
        self.assertIn(document.id, reopened._embeddings)

    def test_content_column_keeps_the_whole_markdown_including_front_matter(self):
        service = self._service()
        document, _ = service.create(
            title="Decision",
            body="Alice chose PostgreSQL.",
            namespace=NAMESPACE,
            kind="decision",
        )
        with self.database.pool.connection() as connection:
            row = connection.execute(
                "SELECT content, namespace, revision FROM core_memory_documents "
                "WHERE memory_id = %s",
                (document.id,),
            ).fetchone()
        self.assertTrue(row["content"].startswith("---\n"))
        self.assertIn("kind: decision", row["content"])
        self.assertIn("sources:", row["content"])
        self.assertEqual(row["namespace"], NAMESPACE)
        self.assertEqual(row["revision"], 1)

    def test_second_writer_on_the_same_revision_loses_with_a_conflict(self):
        first = self._service()
        second = self._service()
        document, _ = first.create(
            title="Shared", body="one writer", namespace=NAMESPACE
        )
        second._load()
        first.update(document.id, body="first wins", expected_revision=1)
        with self.assertRaises(CoreError) as caught:
            second.update(document.id, body="second loses", expected_revision=1)
        self.assertEqual(caught.exception.code, "MEMORY_CONFLICT")
        self.assertEqual(self._service().read(document.id).body, "first wins")

    def test_a_conflicted_writer_can_actually_perform_the_retry_it_is_told_to(self):
        """A stale writer that never reloads makes the prescribed retry a lie."""
        first = self._service()
        second = self._service()
        document, _ = first.create(title="Shared", body="one", namespace=NAMESPACE)
        second._load()
        first.update(document.id, body="two", expected_revision=1)
        with self.assertRaises(CoreError):
            second.update(document.id, body="mine", expected_revision=1)
        # The retry the error prescribes: re-read, then write against what is there.
        current = second.read(document.id)
        self.assertEqual(current.revision, 2)
        self.assertEqual(current.body, "two")
        updated, _ = second.update(
            document.id, body="mine", expected_revision=current.revision
        )
        self.assertEqual(updated.revision, 3)
        self.assertEqual(self._service().read(document.id).body, "mine")

    def test_the_conflict_reports_the_revision_the_store_is_really_at(self):
        first = self._service()
        second = self._service()
        document, _ = first.create(title="Shared", body="one", namespace=NAMESPACE)
        second._load()
        first.update(document.id, body="two", expected_revision=1)
        first.update(document.id, body="three", expected_revision=2)
        with self.assertRaises(CoreError) as caught:
            second.update(document.id, body="mine", expected_revision=1)
        # Deriving it from the number we tried would report the caller's own stale
        # base, which is the one value guaranteed to be useless.
        self.assertEqual(caught.exception.data["current_revision"], 3)

    def test_load_never_pairs_a_new_revision_with_an_old_corpus(self):
        """Under autocommit each SELECT has its own snapshot; order decides."""
        writer = self._service()
        first, _ = writer.create(title="First", body="one", namespace=NAMESPACE)
        reader = MemoryRegistry(self.store).service("core-agent", "user-42")

        real_execute = type(self.database.pool).connection

        # Publish a second revision in the window between the reader's SELECTs.
        original_load = self.store.load
        published = []

        def racing_load(*, app_name, user_id):
            result = original_load(app_name=app_name, user_id=user_id)
            if not published:
                published.append(True)
                writer.create(title="Second", body="two", namespace=NAMESPACE)
            return result

        self.store.load = racing_load
        try:
            reader._load()
        finally:
            self.store.load = original_load
        del real_execute
        # The reader may lag, but it must never believe it is current: its next
        # publish has to collide instead of deleting the other writer's work.
        with self.assertRaises(CoreError) as caught:
            reader.update(first.id, body="clobber", expected_revision=1)
        self.assertEqual(caught.exception.code, "MEMORY_CONFLICT")
        surviving = {
            document.title
            for document in self._service().list_documents().values()
        }
        self.assertEqual(surviving, {"First", "Second"})

    def test_deleted_document_leaves_no_current_row(self):
        service = self._service()
        document, _ = service.create(
            title="Obsolete", body="gone soon", namespace=NAMESPACE
        )
        service.delete(document.id, reason="superseded", expected_revision=1)
        with self.database.pool.connection() as connection:
            current = connection.execute(
                "SELECT count(*) AS n FROM core_memory_documents WHERE memory_id = %s",
                (document.id,),
            ).fetchone()["n"]
            versions = connection.execute(
                "SELECT count(*) AS n FROM core_memory_document_versions "
                "WHERE memory_id = %s",
                (document.id,),
            ).fetchone()["n"]
        self.assertEqual(current, 0)
        self.assertEqual(versions, 1)

    def test_vector_channel_ranks_in_sql_when_the_extension_is_available(self):
        if not self.store.vector_index_available:
            self.skipTest("this PostgreSQL has no pgvector extension")
        service = self._service(KeywordEmbeddings())
        near, _ = service.create(
            title="Near", body="PostgreSQL migration notes.", namespace=NAMESPACE
        )
        service.create(title="Far", body="The cat sat.", namespace=NAMESPACE)
        response = service.search("postgres migration", namespace=NAMESPACE)
        scores = {item.memory_id: item.scores["vector"] for item in response.results}
        self.assertIn(near.id, scores)
        self.assertNotIn("vector", response.degraded_channels)
        self.assertGreater(scores[near.id], 0.5)

    def test_a_vector_of_another_dimension_is_stored_not_rejected(self):
        """The index predicate must exclude it, or the row becomes unwritable."""

        class WideEmbeddings:
            version = "wide-v1"

            def embed(self, text):
                return tuple([0.1] * 8)

        # An index for the configured dimension already exists from setUp.
        registry = MemoryRegistry(self.store, embedding_provider=WideEmbeddings())
        service = registry.service("core-agent", "user-42")
        document, result = service.create(
            title="Wide", body="mismatched vector", namespace=NAMESPACE
        )
        self.assertTrue(result.committed)
        with self.database.pool.connection() as connection:
            row = connection.execute(
                "SELECT array_length(embedding, 1) AS dim FROM core_memory_documents "
                "WHERE memory_id = %s",
                (document.id,),
            ).fetchone()
        self.assertEqual(row["dim"], 8)
        # It simply cannot be ranked by the index built for another dimension.
        self.assertEqual(
            self.store.vector_candidates(
                app_name="core-agent",
                user_id="user-42",
                namespace=NAMESPACE,
                embedding=(0.1, 0.2, 0.3),
                limit=5,
            ),
            {},
        )

    def test_a_missing_vector_extension_degrades_without_breaking_the_store(self):
        original = PostgresMemoryStore._prepare_vector_index
        try:
            PostgresMemoryStore._prepare_vector_index = lambda self: False
            store = PostgresMemoryStore(self.database, embedding_dimension=3)
        finally:
            PostgresMemoryStore._prepare_vector_index = original
        self.assertFalse(store.vector_index_available)
        registry = MemoryRegistry(store, embedding_provider=KeywordEmbeddings())
        service = registry.service("core-agent", "user-42")
        document, _ = service.create(
            title="Near", body="PostgreSQL migration notes.", namespace=NAMESPACE
        )
        response = service.search("postgres migration", namespace=NAMESPACE)
        # No SQL ranking, but the vector is still stored and scored in process.
        self.assertEqual(response.results[0].memory_id, document.id)
        self.assertGreater(response.results[0].scores["vector"], 0.5)
        self.assertIsNone(
            store.vector_candidates(
                app_name="core-agent",
                user_id="user-42",
                namespace=NAMESPACE,
                embedding=(1.0, 0.0, 0.0),
                limit=5,
            )
        )


if __name__ == "__main__":
    unittest.main()
