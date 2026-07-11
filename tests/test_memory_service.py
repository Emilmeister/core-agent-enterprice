import re
import tempfile
import unittest
from pathlib import Path

from memory_service.errors import MemoryServiceError
from memory_service.service import MemoryService


def markdown(
    body_lines,
    *,
    memory_id="mem-1",
    title="Project facts",
    namespace="session/context-1",
    kind="fact",
    status="active",
):
    if isinstance(body_lines, str):
        body = body_lines
    else:
        body = "\n".join(body_lines)
    return (
        "---\n"
        f"id: {memory_id}\n"
        f"title: {title}\n"
        f"namespace: {namespace}\n"
        f"kind: {kind}\n"
        f"status: {status}\n"
        "created_at: 2026-07-11T10:00:00Z\n"
        "updated_at: 2026-07-11T10:00:00Z\n"
        "sources:\n"
        "  - task_id: task-1\n"
        "    event_revision: 1\n"
        "---\n"
        f"{body}\n"
    )


class ToggleExtractor:
    version = "test-ner-v1"

    def __init__(self):
        self.fail = False

    def extract(self, text):
        if self.fail:
            raise RuntimeError("NER unavailable")
        entities = []
        for match in re.finditer(r"\b[A-Z][A-Za-z0-9_-]+\b", text):
            entities.append(
                {
                    "text": match.group(0),
                    "type": "proper_name",
                    "start": match.start(),
                    "end": match.end(),
                    "confidence": 0.99,
                }
            )
        return {"entities": entities, "relations": []}


class MemoryLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = MemoryService(Path(self.temp.name))
        self.service.search("Project facts", namespace="session/context-1")

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_exactly_two_hundred_body_lines_are_allowed(self):
        content = markdown([f"line {i}" for i in range(200)])
        result = self.service.create("session/project.md", content, expected_repository_revision=0)
        self.assertTrue(result.committed)
        self.assertEqual(result.repository_revision, 1)
        self.assertEqual(self.service.read("mem-1").body_line_count, 200)

    def test_create_over_two_hundred_lines_is_hard_rejected_without_side_effect(self):
        content = markdown([f"line {i}" for i in range(201)])
        before = self.service.repository_revision
        with self.assertRaises(MemoryServiceError) as caught:
            self.service.create("session/project.md", content, expected_repository_revision=before)
        error = caught.exception
        self.assertEqual(error.code, "MEMORY_FILE_TOO_LARGE")
        self.assertEqual(error.data["actual_body_lines"], 201)
        self.assertEqual(error.data["max_body_lines"], 200)
        self.assertEqual(error.data["recommended_action"], "split_into_multiple_markdown_files")
        self.assertTrue(error.data["suggested_boundaries"])
        self.assertFalse(error.data["committed"])
        self.assertEqual(self.service.repository_revision, before)
        self.assertEqual(self.service.list_documents(), ())
        self.assertFalse((Path(self.temp.name) / "session" / "project.md").exists())

    def test_update_over_limit_does_not_change_file_or_indexes(self):
        created = self.service.create(
            "session/project.md",
            markdown(["original"]),
            expected_repository_revision=0,
        )
        before_doc = self.service.read("mem-1")
        before_index = self.service.index_snapshot()
        with self.assertRaises(MemoryServiceError) as caught:
            self.service.update(
                "mem-1",
                {"replace_content": markdown([f"new {i}" for i in range(201)])},
                expected_file_revision=before_doc.revision,
            )
        self.assertEqual(caught.exception.code, "MEMORY_FILE_TOO_LARGE")
        self.assertEqual(self.service.repository_revision, created.repository_revision)
        self.assertEqual(self.service.read("mem-1"), before_doc)
        self.assertEqual(self.service.index_snapshot(), before_index)

    def test_rejection_never_truncates_or_automatically_splits(self):
        content = markdown([f"unique-{i}" for i in range(237)])
        with self.assertRaises(MemoryServiceError):
            self.service.create("session/large.md", content, expected_repository_revision=0)
        self.assertEqual(self.service.list_documents(), ())
        self.assertEqual(tuple(Path(self.temp.name).rglob("*.md")), ())


class MemoryMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.extractor = ToggleExtractor()
        self.service = MemoryService(Path(self.temp.name), entity_extractor=self.extractor)
        self.service.search("Alice Acme", namespace="session/context-1")

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def _create(self, body="Alice founded Acme."):
        return self.service.create(
            "session/project.md",
            markdown(body),
            expected_repository_revision=self.service.repository_revision,
        )

    def test_successful_write_atomically_updates_every_index_and_ner(self):
        result = self._create()
        self.assertEqual(result.repository_revision, result.index_revision)
        status = self.service.index_status(result.repository_revision)
        self.assertEqual(
            status.components,
            {
                "markdown": "ready",
                "chunks": "ready",
                "bm25": "ready",
                "vectors": "ready",
                "ner": "ready",
                "graph": "ready",
            },
        )
        self.assertEqual(self.service.graph_mentions("Alice"), ("mem-1",))
        self.assertEqual(self.service.graph_mentions("Acme"), ("mem-1",))

    def test_changed_chunk_removes_stale_entity_edges(self):
        self._create()
        document = self.service.read("mem-1")
        result = self.service.update(
            "mem-1",
            {"replace_content": markdown("Bob founded Beta.")},
            expected_file_revision=document.revision,
        )
        self.assertTrue(result.committed)
        self.assertEqual(self.service.graph_mentions("Alice"), ())
        self.assertEqual(self.service.graph_mentions("Acme"), ())
        self.assertEqual(self.service.graph_mentions("Bob"), ("mem-1",))
        self.assertEqual(self.service.graph_mentions("Beta"), ("mem-1",))

    def test_index_failure_keeps_previous_committed_revision_visible(self):
        self._create()
        before = self.service.read("mem-1")
        before_index = self.service.index_snapshot()
        self.extractor.fail = True
        with self.assertRaises(MemoryServiceError) as caught:
            self.service.update(
                "mem-1",
                {"replace_content": markdown("Bob founded Beta.")},
                expected_file_revision=before.revision,
            )
        self.assertEqual(caught.exception.code, "MEMORY_INDEX_FAILED")
        self.assertEqual(self.service.read("mem-1"), before)
        self.assertEqual(self.service.index_snapshot(), before_index)
        self.assertEqual(self.service.graph_mentions("Alice"), ("mem-1",))

    def test_optimistic_concurrency_rejects_stale_update(self):
        self._create()
        revision = self.service.read("mem-1").revision
        self.service.update(
            "mem-1",
            {"replace_content": markdown("Alice joined Beta.")},
            expected_file_revision=revision,
        )
        with self.assertRaises(MemoryServiceError) as caught:
            self.service.update(
                "mem-1",
                {"replace_content": markdown("stale")},
                expected_file_revision=revision,
            )
        self.assertEqual(caught.exception.code, "MEMORY_CONFLICT")
        self.assertIn("current_revision", caught.exception.data)

    def test_explicit_split_is_atomic_and_all_children_obey_limit(self):
        self._create("# Overview\nshort")
        original = self.service.read("mem-1")
        plan = {
            "overview": {
                "path": "session/project.md",
                "content": markdown("# Overview\nSee [[mem-2]] and [[mem-3]]."),
            },
            "children": [
                {
                    "path": "session/project-decisions.md",
                    "content": markdown(
                        ["# Decisions", *[f"decision {i}" for i in range(150)]],
                        memory_id="mem-2",
                        title="Project decisions",
                        kind="decision",
                    ),
                },
                {
                    "path": "session/project-questions.md",
                    "content": markdown(
                        ["# Questions", *[f"question {i}" for i in range(100)]],
                        memory_id="mem-3",
                        title="Project questions",
                    ),
                },
            ],
        }
        result = self.service.split("mem-1", plan, expected_file_revision=original.revision)
        self.assertTrue(result.committed)
        self.assertEqual({doc.id for doc in self.service.list_documents()}, {"mem-1", "mem-2", "mem-3"})
        self.assertTrue(all(doc.body_line_count <= 200 for doc in self.service.list_documents()))
        self.assertIn("mem-2", self.service.explicit_links("mem-1"))

        before = self.service.index_snapshot()
        bad_plan = {
            "overview": plan["overview"],
            "children": [
                {
                    "path": "session/too-large.md",
                    "content": markdown([str(i) for i in range(201)], memory_id="mem-4"),
                }
            ],
        }
        with self.assertRaises(MemoryServiceError) as caught:
            self.service.split("mem-1", bad_plan, expected_file_revision=self.service.read("mem-1").revision)
        self.assertEqual(caught.exception.code, "MEMORY_FILE_TOO_LARGE")
        self.assertEqual(self.service.index_snapshot(), before)
        self.assertNotIn("mem-4", {doc.id for doc in self.service.list_documents()})

    def test_delete_removes_canonical_and_all_derived_data(self):
        self._create()
        revision = self.service.read("mem-1").revision
        self.service.delete("mem-1", "forget", expected_file_revision=revision)
        with self.assertRaises(MemoryServiceError) as caught:
            self.service.read("mem-1")
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.assertEqual(self.service.graph_mentions("Alice"), ())
        self.assertEqual(self.service.search("Alice", namespace="session/context-1").results, ())


class HybridRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = MemoryService(Path(self.temp.name), entity_extractor=ToggleExtractor())
        self.service.search("bootstrap", namespace="session/context-1")
        self.service.create(
            "session/acme.md",
            markdown("Alice founded Acme database platform.", memory_id="mem-acme", title="Acme"),
            expected_repository_revision=0,
        )
        self.service.search("bootstrap", namespace="session/context-1")
        self.service.create(
            "session/beta.md",
            markdown("Bob maintains Beta analytics.", memory_id="mem-beta", title="Beta"),
            expected_repository_revision=1,
        )

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_search_uses_bm25_vectors_graph_and_rerank_with_provenance(self):
        response = self.service.search("Who founded Acme?", namespace="session/context-1", limit=5)
        self.assertEqual(response.results[0].memory_id, "mem-acme")
        self.assertEqual(set(response.results[0].scores), {"bm25", "vector", "graph", "final"})
        self.assertTrue(response.results[0].graph_paths)
        self.assertEqual(response.results[0].revision, self.service.repository_revision)
        self.assertEqual(response.results[0].provenance["path"], "session/acme.md")
        self.assertEqual(set(response.component_versions), {"bm25", "embedding", "graph", "reranker", "ner"})
        self.assertTrue(response.candidate_set)

    def test_degraded_channel_is_explicit(self):
        self.service.set_channel_available("graph", False, reason="reindexing")
        response = self.service.search("Acme", namespace="session/context-1")
        self.assertEqual(response.degraded_channels, {"graph": "reindexing"})
        self.assertIsNone(response.results[0].scores["graph"])
        self.assertIsNotNone(response.results[0].scores["final"])

    def test_rebuild_from_markdown_restores_equivalent_searchable_graph(self):
        before_search = self.service.search("Acme", namespace="session/context-1").to_dict()
        before_mentions = self.service.graph_mentions("Alice")
        self.service.drop_derived_indexes()
        self.service.rebuild()
        after_search = self.service.search("Acme", namespace="session/context-1").to_dict()
        self.assertEqual(after_search, before_search)
        self.assertEqual(self.service.graph_mentions("Alice"), before_mentions)

    def test_manual_entity_resolution_survives_rebuild(self):
        self.service.entity_resolve("entity:Acme", "entity:Acme_Corp", evidence="confirmed by task-1")
        self.service.drop_derived_indexes()
        self.service.rebuild()
        self.assertEqual(self.service.resolve_entity("Acme"), "entity:Acme_Corp")


if __name__ == "__main__":
    unittest.main()
