"""Authenticated memory isolation, including direct identifiers and shared stores."""
import asyncio
import json
import os
import unittest
import uuid
from unittest.mock import patch

import httpx
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier
from psycopg.types.json import Jsonb

from core_agent import database as database_module
from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.memory import MemoryRegistry
from core_agent.memory_store import InMemoryMemoryStore, PostgresMemoryStore
from core_agent.owner_api import policy_catalog
from core_agent.workflow import PostgresWorkflowStore
from tests.app_support import create_app
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL
from tests.test_memory_service import KeywordEmbeddings


class MemoryScopeTests(AuthAppTestCase):
    def memory_app(self, **kwargs):
        with patch.dict(os.environ, {
            "CORE_AGENT_MEMORY": "optional",
            "MEMORY_STORAGE_TYPE": "postgres" if self.use_postgres else "in-memory",
            "SESSION_STORAGE_TYPE": "postgres" if self.use_postgres else "in-memory",
        }):
            return create_app(**kwargs)

    async def asyncSetUp(self):
        with patch("tests.test_auth.create_app", side_effect=self.memory_app):
            await super().asyncSetUp()
        self.agent = self.app.state.core_agent
        self.tenant = self.app.state.authenticator.settings.tenant
        self.other_model = ScriptedModel([])
        self.other_model.model = self.model.model
        with patch.dict(os.environ, {"CORE_AGENT_TENANT_ID": self.tenant + "-other"}):
            self.other_app = self.memory_app(
                model=self.other_model, base_url="https://agent.example.test",
                auth_transport=httpx.MockTransport(self.introspect),
                database=PostgresDatabase(TEST_DATABASE_URL) if self.use_postgres else None,
            )
        self.addCleanup(self.other_app.state.close)
        self.other_agent = self.other_app.state.core_agent
        if self.use_postgres:
            for agent in (self.agent, self.other_agent):
                self.assertIsInstance(agent.workflow_store, PostgresWorkflowStore)
        else:
            # Independent application registries, one shared persistent corpus.
            self.other_agent.memory_registry.store = self.agent.memory_registry.store
        for name, origin in policy_catalog(self.other_agent).items():
            self.other_agent.interaction_store.update_policy(
                self.tenant + "-other", name, origin, mode="allow",
                guardrails_exempt=False, expected_revision=0, actor_id="test-fixture-owner",
            )
        self.other_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.other_app),
            base_url="https://agent.example.test",
        )
        self.addAsyncCleanup(self.other_http.aclose)

    async def tool(self, name, arguments, *, other=False, context=None, token="owner-a"):
        # A cold worker must scope durable reads, including IDs created
        # after an earlier worker loaded its corpus.
        (self.other_agent if other else self.agent).memory_registry._services.clear()
        model = self.other_model if other else self.model
        model._responses = [ModelResponse(tool_requests=(ToolRequest("memory-call", name, arguments),)),
                            ModelResponse(message="done")]
        kind = "owner" if token.startswith("owner") else "external"
        response = await (self.other_http if other else self.http).post(
            f"/a2a/{kind}/message:send", headers=self.headers(token),
            json={"message": {"messageId": uuid.uuid4().hex,
                  "contextId": context or uuid.uuid4().hex, "role": "ROLE_USER",
                  "parts": [{"text": "Exercise the configured memory tool"}]}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        agent = self.other_agent if other else self.agent
        task = response.json()["task"]
        record = agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        self.assertEqual(record.tenant_id, self.tenant + "-other" if other else self.tenant)
        result = next(json.loads(item["content"]) for item in record.snapshot["context"]["transcript"]
                      if item["kind"] == "tool_result")
        self.assertEqual(result["tool_name"], name)
        return result, record

    @staticmethod
    def id_arguments(operation, created, *, scope="user"):
        arguments = {"memory_id": created["memory_id"], "scope": scope}
        if operation != "read":
            arguments["expected_revision"] = created["revision"]
        if operation == "update":
            arguments["body"] = "unauthorized replacement"
        elif operation == "split":
            arguments.update(overview={"body": "unauthorized overview"},
                             children=[{"title": "Unauthorized child", "body": "replacement"}])
        elif operation == "delete":
            arguments["reason"] = "unauthorized deletion"
        return arguments

    async def test_two_companies_with_same_owner_cannot_search_or_use_direct_ids(self):
        for operation in ("search", "read", "update", "split", "delete"):
            with self.subTest(operation=operation):
                created, _ = await self.tool("core_memory_create", {"title": "Tenant note",
                    "body": "uniquecompanysecret " + operation})
                self.assertEqual(created["status"], "succeeded")
                arguments = ({"query": "uniquecompanysecret"} if operation == "search"
                             else self.id_arguments(operation, created["output"]))
                result, _ = await self.tool("core_memory_" + operation, arguments, other=True)
                if operation == "search":
                    self.assertEqual(result["output"]["results"], [])
                else:
                    self.assertEqual(result.get("error_code"), "NOT_FOUND")
                original, _ = await self.tool("core_memory_read", self.id_arguments("read", created["output"]))
                self.assertEqual(original["output"]["body"], "uniquecompanysecret " + operation)
                self.assertEqual(original["output"]["revision"], 1)

    async def test_session_direct_ids_never_read_or_mutate_other_session(self):
        for operation in ("read", "update", "split", "delete"):
            with self.subTest(operation=operation):
                context = uuid.uuid4().hex
                created, _ = await self.tool("core_memory_create", {"title": "Session note",
                    "body": "sessionprivate " + operation, "scope": "session"}, context=context)
                wrong_namespace, _ = await self.tool("core_memory_" + operation,
                    self.id_arguments(operation, created["output"], scope="user"), context=context)
                self.assertEqual(wrong_namespace.get("error_code"), "NOT_FOUND")
                result, _ = await self.tool("core_memory_" + operation,
                    self.id_arguments(operation, created["output"], scope="session"))
                self.assertEqual(result.get("error_code"), "NOT_FOUND")
                original, _ = await self.tool("core_memory_read",
                    self.id_arguments("read", created["output"], scope="session"), context=context)
                self.assertEqual(original["output"]["body"], "sessionprivate " + operation)
                self.assertEqual(original["output"]["revision"], 1)

    async def test_owner_sharing_and_external_token_rotation_keep_exact_identity(self):
        owner, _ = await self.tool("core_memory_create", {"title": "Shared", "body": "owner shared fact"})
        shared, _ = await self.tool("core_memory_read", self.id_arguments("read", owner["output"]), token="owner-b")
        self.assertEqual(shared["output"]["body"], "owner shared fact")
        external, _ = await self.tool("core_memory_create", {"title": "External", "body": "external stable fact"}, token="external-a")
        rotated, _ = await self.tool("core_memory_read", self.id_arguments("read", external["output"]), token="external-a-replaced")
        self.assertEqual(rotated["output"]["body"], "external stable fact")
        foreign, _ = await self.tool("core_memory_read", self.id_arguments("read", external["output"]), token="external-b")
        self.assertEqual(foreign.get("error_code"), "NOT_FOUND")

    async def test_enterprise_missing_trusted_tenant_or_identity_cannot_fall_back(self):
        for scope in ({"identity": "company-owners"}, {"tenant_id": self.tenant},
                      {"tenant_id": "", "identity": "company-owners"},
                      {"tenant_id": self.tenant, "identity": ""},
                      {"tenant_id": self.tenant, "identity": "anonymous"}):
            with self.subTest(keys=tuple(scope)):
                self.agent._run_scopes["missing-scope"] = scope
                with self.assertRaises(CoreError) as caught:
                    self.agent._memory_create({"title": "Refused", "body": "private"}, "missing-scope")
                self.assertEqual(caught.exception.code, "AUTHENTICATION_REQUIRED")

    async def test_explicit_child_memory_inherits_authenticated_parent_scope(self):
        context = uuid.uuid4().hex
        user, _ = await self.tool("core_memory_create", {
            "title": "Parent user", "body": "parent tenant fact"}, context=context)
        session, _ = await self.tool("core_memory_create", {
            "title": "Parent session", "body": "parent session fact", "scope": "session"}, context=context)
        foreign, _ = await self.tool("core_memory_create", {
            "title": "Other company", "body": "foreign private fact"}, other=True, context=context)
        self.model._responses = [
            ModelResponse(tool_requests=(ToolRequest("delegate-memory", "core_delegate", {
                "instruction": "Read the delegated parent memory records",
                "tools": ["core_memory_read"], "skills": [],
                "budget": {"turns": 2, "tool_calls": 3},
            }),)),
            ModelResponse(tool_requests=(
                ToolRequest("read-user", "core_memory_read", self.id_arguments("read", user["output"])),
                ToolRequest("read-session", "core_memory_read", self.id_arguments("read", session["output"], scope="session")),
                ToolRequest("read-foreign", "core_memory_read", self.id_arguments("read", foreign["output"])),
            )),
            ModelResponse(message="child memory checked"),
            ModelResponse(message="parent memory checked"),
        ]
        offset = len(self.model.calls)
        task = await self.submit("owner-a", uuid.uuid4().hex, context)
        parent = self.agent.workflow_store.lookup_task(task["id"])
        children = self.agent.task_scheduler.list(owner_id=parent.run_id, tenant_id=self.tenant)
        self.assertEqual(len(children), 1)
        settled = await asyncio.to_thread(self.agent.task_scheduler.wait, children[0].id,
            timeout=10, owner_id=parent.run_id, tenant_id=self.tenant)
        self.assertEqual(settled.state, "completed", settled.error)
        with patch.object(self.agent, "_launch_recovery", return_value=False):
            await asyncio.to_thread(self.agent._recover_workflows_once)
        result = await asyncio.to_thread(self.agent.resume_task, parent.task_id)
        self.assertEqual(result.message, "parent memory checked")
        child = self.agent.workflow_store.lookup_task(children[0].id)
        self.assertEqual(child.state, "COMPLETED", child.error_code)
        self.assertEqual((child.tenant_id, child.owner_id, child.context_id, child.parent_run_id),
            (parent.tenant_id, parent.owner_id, parent.context_id, parent.run_id))
        outcomes = {value["tool_call_id"]: value for item in child.snapshot["context"]["transcript"]
            if item["kind"] == "tool_result" for value in [json.loads(item["content"])]}
        for call_id, created, body in (("read-user", user, "parent tenant fact"),
                                      ("read-session", session, "parent session fact")):
            self.assertEqual(outcomes[call_id]["status"], "succeeded")
            self.assertEqual((outcomes[call_id]["output"]["memory_id"], outcomes[call_id]["output"]["body"]),
                (created["output"]["memory_id"], body))
        self.assertEqual(outcomes["read-foreign"]["error_code"], "NOT_FOUND")
        calls = self.model.calls[offset:]
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[1].tools, frozenset({"core_memory_read"}))
        self.assertEqual(calls[2].tools, frozenset())  # Reserved final turn after the exact child tool budget.


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresMemoryScopeTests(MemoryScopeTests):
    use_postgres = True


class MemoryCorpusScopeTests(unittest.TestCase):
    use_postgres = False

    def setUp(self):
        if self.use_postgres:
            self.database = PostgresDatabase(TEST_DATABASE_URL)
            self.addCleanup(self.database.close)
            self.store = PostgresMemoryStore(self.database, embedding_dimension=3)
        else:
            self.store = InMemoryMemoryStore()
        self.registry = MemoryRegistry(self.store, embedding_provider=KeywordEmbeddings())
        self.addCleanup(self.registry.close)
        self.app_name = "memory-scope-" + uuid.uuid4().hex
        self.user = "company-owners"
        self.namespace = "subject/" + self.user

    def service(self, tenant):
        return self.registry.service(self.app_name, self.user, tenant_id=tenant)

    def load(self, tenant):
        return self.store.load(tenant_id=tenant, app_name=self.app_name, user_id=self.user)

    def test_cache_indexes_history_and_revision_conflicts_are_tenant_scoped_after_restart(self):
        first, second = self.service("company-a"), self.service("company-b")
        self.assertIsNot(first, second)
        a, _ = first.create(title="Postgres A", body="postgres migration Alpha", namespace=self.namespace)
        b, _ = second.create(title="Postgres B", body="postgres migration Beta", namespace=self.namespace)
        first.update(a.id, body="postgres migration Alpha revised", expected_revision=1)
        first.entity_resolve("Alpha", "Alpha canonical")
        loaded_b = self.load("company-b")
        with self.assertRaises(CoreError) as caught:
            self.store.publish(tenant_id="company-b", app_name=self.app_name, user_id=self.user,
                repository_revision=loaded_b.repository_revision,
                documents=loaded_b.documents, resolutions=loaded_b.resolutions)
        self.assertEqual(caught.exception.code, "MEMORY_CONFLICT")
        self.assertEqual(caught.exception.data["current_revision"], 1)
        fresh = MemoryRegistry(self.store, embedding_provider=KeywordEmbeddings())
        reloaded = fresh.service(self.app_name, self.user, tenant_id="company-b")
        self.assertEqual(set(reloaded.list_documents()), {b.id})
        self.assertEqual(reloaded.repository_revision, 1)
        self.assertEqual(len(reloaded.history(b.id)), 1)
        self.assertEqual(reloaded._resolutions, {})
        self.assertEqual({item.memory_id for item in reloaded.search("postgres migration", namespace=self.namespace).results}, {b.id})
        with self.assertRaises(CoreError) as caught:
            reloaded.read(a.id, namespace=self.namespace)
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        if self.use_postgres:
            self.assertTrue(self.store.vector_index_available)
            ranked = self.store.vector_candidates(tenant_id="company-b", app_name=self.app_name,
                user_id=self.user, namespace=self.namespace, embedding=(1.0, 1.0, 0.5), limit=10)
            self.assertEqual(set(ranked), {b.id})

    def test_legacy_empty_tenant_is_quarantined_from_default_and_enterprise_corpora(self):
        legacy, _ = self.service("fixture-source").create(title="Legacy", body="legacy private note", namespace=self.namespace)
        loaded = self.load("fixture-source")
        self.store.publish(tenant_id="", app_name=self.app_name, user_id=self.user,
            repository_revision=loaded.repository_revision, documents=loaded.documents,
            resolutions=loaded.resolutions)
        self.assertEqual(self.load("").documents[0].memory_id, legacy.id)
        for service in (self.registry.service(self.app_name, self.user), self.service("company-a")):
            self.assertEqual(service.list_documents(), {})
            self.assertEqual(service.repository_revision, 0)
            self.assertEqual(service.search("legacy", namespace=self.namespace).results, ())
            with self.assertRaises(CoreError) as caught:
                service.read(legacy.id, namespace=self.namespace)
            self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.registry.service(self.app_name, self.user).create(title="New default", body="separate", namespace=self.namespace)
        self.assertEqual(self.load("").documents, loaded.documents)
        self.assertEqual(self.load("").repository_revision, 1)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresMemoryCorpusScopeTests(MemoryCorpusScopeTests):
    use_postgres = True

    def test_schema23_upgrade_preserves_and_quarantines_all_legacy_rows(self):
        schema = "memory_upgrade_" + uuid.uuid4().hex
        with self.database.transaction() as connection:
            connection.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
        def remove_schema():
            with self.database.transaction() as connection:
                connection.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))
        self.addCleanup(remove_schema)
        upgraded = PostgresDatabase(make_conninfo(TEST_DATABASE_URL, options="-c search_path=" + schema))
        self.addCleanup(upgraded.close)
        old_migrations = {version: sql for version, sql in database_module.MIGRATIONS.items() if version <= 23}
        with patch.object(database_module, "SCHEMA_VERSION", 23), \
                patch.dict(database_module.MIGRATIONS, old_migrations, clear=True):
            upgraded.migrate()
            self.assertEqual(upgraded.schema_version(), 23)
        service = MemoryRegistry(InMemoryMemoryStore()).service("core-agent", "company-owners")
        document, _ = service.create(title="Legacy Unicode", body="Exact private bytes: ё\nline two\n",
                                     namespace=self.namespace)
        tables = ("core_memory_documents", "core_memory_document_versions", "core_memory_revisions")
        with upgraded.transaction() as connection:
            connection.execute("INSERT INTO core_memory_documents (app_name,user_id,memory_id,namespace,path,content,revision,embedding,entities) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                ("core-agent", self.user, document.id, document.namespace, document.path,
                 document.content, 1, [0.25, 0.5], Jsonb([{"text": "ё", "type": "legacy"}])))
            connection.execute("INSERT INTO core_memory_document_versions (app_name,user_id,memory_id,namespace,path,content,revision) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                ("core-agent", self.user, document.id, document.namespace, document.path, document.content, 1))
            connection.execute("INSERT INTO core_memory_revisions (app_name,user_id,repository_revision,resolutions) VALUES (%s,%s,%s,%s)",
                ("core-agent", self.user, 1, Jsonb({"legacy": "entity:ё"})))
            before = {table: connection.execute(SQL("SELECT * FROM {}").format(Identifier(table))).fetchone()
                      for table in tables}
        upgraded.migrate()
        upgraded.migrate()
        self.assertEqual(upgraded.schema_version(), 26)
        with upgraded.pool.connection() as connection:
            for table in tables:
                row = connection.execute(SQL("SELECT * FROM {}").format(Identifier(table))).fetchone()
                self.assertEqual(row.pop("tenant_id"), "")
                self.assertEqual(row, before[table])
            self.assertEqual(connection.execute("SELECT content FROM core_memory_documents").fetchone()["content"].encode(), document.content.encode())
        with patch.object(database_module, "SCHEMA_VERSION", 23):
            with self.assertRaises(CoreError) as caught:
                upgraded.verify_schema()
            self.assertEqual(caught.exception.code, "DATABASE_SCHEMA_MISMATCH")
        upgraded.verify_schema()
        registry = MemoryRegistry(PostgresMemoryStore(upgraded))
        for selected in (registry.service("core-agent", self.user),
                         registry.service("core-agent", self.user, tenant_id="company-a")):
            self.assertEqual(selected.list_documents(), {})
            self.assertEqual(selected.repository_revision, 0)
            with self.assertRaises(CoreError) as caught:
                selected.read(document.id, namespace=self.namespace)
            self.assertEqual(caught.exception.code, "NOT_FOUND")
