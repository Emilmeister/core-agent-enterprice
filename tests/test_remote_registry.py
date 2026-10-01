import os
import threading
import unittest
import uuid

from cryptography.fernet import Fernet

from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.remote_registry import InMemoryRemoteRegistry, PostgresRemoteRegistry


class RemoteRegistryContract:
    def values(self, **changes):
        return {"name": "peer", "url": "https://peer.example/a2a", "description": "Peer",
                "enabled": True, "header_name": "Authorization", **changes}

    def create(self, **changes):
        values = self.values()
        values.update(changes)
        return self.store.create(self.tenant, values, actor_id="owner")

    def update(self, peer, **changes):
        values = {key: peer[key] for key in ("url", "description", "enabled", "header_name")}
        values.update(changes)
        return self.store.update(self.tenant, peer["id"], values,
                                 expected_revision=peer["revision"], actor_id="editor")

    def test_metadata_scope_unique_name_and_immutable_revisions(self):
        peer = self.create(header_value="Bearer PRIVATE")
        self.assertEqual(set(peer), {"id", "name", "url", "description", "enabled",
                                     "header_name", "has_header_value", "revision"})
        uuid.UUID(peer["id"])
        self.assertEqual(peer["revision"], 1)
        self.assertNotIn("PRIVATE", repr(peer))
        self.assertEqual(self.store.resolve_headers(self.tenant, peer["id"], 1),
                         {"Authorization": "Bearer PRIVATE"})
        with self.assertRaises(CoreError) as error:
            self.create()
        self.assertEqual(error.exception.code, "REMOTE_AGENT_CONFLICT")
        with self.assertRaises(CoreError) as error:
            self.store.get_revision(self.tenant + "other", peer["id"])
        self.assertEqual(error.exception.code, "REMOTE_AGENT_NOT_FOUND")
        changed = self.update(peer, header_name="X-Api-Key", url="https://other.example")
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(self.store.get_revision(self.tenant, peer["id"], 1), peer)
        self.assertEqual(self.store.resolve_headers(self.tenant, peer["id"], 2),
                         {"X-Api-Key": "Bearer PRIVATE"})
        cleared = self.update(changed, header_value=None)
        self.assertFalse(cleared["has_header_value"])
        self.assertEqual(self.store.resolve_headers(self.tenant, peer["id"], 3), {})
        self.assertEqual(self.store.resolve_headers(self.tenant, peer["id"], 1),
                         {"Authorization": "Bearer PRIVATE"})

    def test_disable_and_cas(self):
        peer = self.create(header_value="SECRET")
        disabled = self.store.disable(self.tenant, peer["id"], expected_revision=1, actor_id="owner")
        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["revision"], 2)
        self.assertEqual(self.store.resolve_headers(self.tenant, peer["id"], 1), {"Authorization": "SECRET"})
        with self.assertRaises(CoreError) as error:
            self.store.disable(self.tenant, peer["id"], expected_revision=1, actor_id="owner")
        self.assertEqual(error.exception.code, "REMOTE_AGENT_CONFLICT")

    def test_list_stable_pagination_and_detached_metadata(self):
        peers = [self.create(name="peer" + str(index)) for index in range(3)]
        expected = sorted(peers, key=lambda peer: peer["id"])
        first = self.store.list(self.tenant, limit=2)
        self.assertEqual(first, expected[:2])
        self.assertEqual(self.store.list(self.tenant, after_id=first[-1]["id"]), expected[2:])
        self.assertEqual(self.store.list(self.tenant + "other"), [])
        first[0]["enabled"] = False
        self.assertTrue(self.store.get_revision(self.tenant, first[0]["id"])["enabled"])
        for limit in (0, 102, True):
            with self.assertRaises(CoreError):
                self.store.list(self.tenant, limit=limit)
        for tenant, cursor in ((self.tenant, "missing"), (self.tenant + "other", first[0]["id"])):
            with self.assertRaises(CoreError) as error:
                self.store.list(tenant, after_id=cursor)
            self.assertEqual(error.exception.code, "REQUEST_INVALID")

    def test_ciphertext_scope_revision_header_binding_and_audit(self):
        peer = self.create(header_value="SECRET")
        second = self.create(name="second", header_value="OTHER")
        updated = self.update(peer, header_name="X-Key")
        first_row = self.raw(peer["id"], 1)
        updated_row = self.raw(peer["id"], 2)
        self.assertEqual((first_row["actor_id"], updated_row["actor_id"]), ("owner", "editor"))
        self.assertGreaterEqual(updated_row["created_at"], first_row["created_at"])
        self.assertNotIn("SECRET", repr(first_row))
        self.assertNotIn("SECRET", repr(self.store))
        self.assertNotEqual(first_row["encrypted_payload"], updated_row["encrypted_payload"])
        self.tamper(updated["id"], 2, "encrypted_payload", first_row["encrypted_payload"])
        self.tamper(second["id"], 1, "encrypted_payload", first_row["encrypted_payload"])
        for identity, revision in ((peer["id"], 2), (second["id"], 1)):
            with self.assertRaises(CoreError) as error:
                self.store.resolve_headers(self.tenant, identity, revision)
            self.assertEqual(error.exception.code, "REMOTE_AGENT_CREDENTIAL_UNAVAILABLE")
            self.assertNotIn("SECRET", str(error.exception))
        self.tamper(peer["id"], 1, "header_name", "X-Changed")
        with self.assertRaises(CoreError) as error:
            self.store.resolve_headers(self.tenant, peer["id"], 1)
        self.assertEqual(error.exception.code, "REMOTE_AGENT_CREDENTIAL_UNAVAILABLE")

    def test_concurrent_create_name_has_one_winner(self):
        barrier = threading.Barrier(3)
        results = []

        def create():
            barrier.wait(timeout=5)
            try:
                results.append(self.create()["revision"])
            except CoreError as error:
                results.append(error.code)

        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, [1, "REMOTE_AGENT_CONFLICT"])

    def test_boundary_validation_never_echoes_secret(self):
        invalid = [("name", "a b"), ("name", "é"), ("enabled", 1),
                   ("url", "https:///missing"), ("url", "https://x:70000"),
                   ("url", "https://x?q=1"), ("url", "https://x#f"),
                   ("url", "https://user@x"), ("url", "https://x/\n"),
                   ("url", "https://x/\x80"),
                   ("url", "http://external.example"), ("url", "https://x/\ud800"),
                   ("description", "\ud800"), ("description", "é" * 2049),
                   ("header_name", "Host"), ("header_name", "x:bad"),
                   ("header_value", ""), ("header_value", "SECRET\r\n"),
                   ("header_value", "SECRET\x7f"), ("header_value", "SECRET☃"),
                   ("header_value", "x" * 16385)]
        for field, value in invalid:
            with self.subTest(field=field, value=repr(value)[:50]):
                with self.assertRaises(CoreError) as error:
                    self.create(**{field: value})
                self.assertEqual(error.exception.code, "REMOTE_AGENT_INVALID")
                self.assertNotIn("SECRET", str(error.exception))
        for name in ("Content-Type", "Content-Length", "Connection", "Transfer-Encoding",
                     "Upgrade", "Trailer", "TE", "Proxy-Authorization", "Accept", "A2A-Version"):
            with self.subTest(name=name), self.assertRaises(CoreError):
                self.create(header_name=name)
        peer = self.create(url="http://127.0.0.1:8080/a2a", header_value=None)
        self.assertFalse(peer["has_header_value"])
        with self.assertRaises(CoreError):
            self.update(peer, name="changed")

    def test_description_rejects_nul_on_create_and_update_without_mutation(self):
        peer = self.create(description="first\nsecond\tcolumn")
        for mutation in (
            lambda: self.create(name="invalid", description="before\x00after"),
            lambda: self.update(peer, description="before\x00after"),
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(CoreError) as error:
                    mutation()
                self.assertEqual(error.exception.code, "REMOTE_AGENT_INVALID")
        self.assertEqual(self.store.list(self.tenant), [peer])
        self.assertEqual(self.store.get_revision(self.tenant, peer["id"]), peer)
        updated = self.update(peer, description="updated\ntext\tcolumn")
        self.assertEqual(updated["description"], "updated\ntext\tcolumn")

    def test_peer_ids_and_cursors_validate_before_storage_lookup(self):
        peer = self.create()
        values = {key: peer[key] for key in ("url", "description", "enabled", "header_name")}
        operations = {
            "get": lambda identity: self.store.get_revision(self.tenant, identity),
            "revision": lambda identity: self.store.get_revision(self.tenant, identity, 1),
            "headers": lambda identity: self.store.resolve_headers(self.tenant, identity, 1),
            "update": lambda identity: self.store.update(self.tenant, identity, values,
                                                         expected_revision=1, actor_id="owner"),
            "disable": lambda identity: self.store.disable(self.tenant, identity,
                                                           expected_revision=1, actor_id="owner"),
        }
        malformed = ("\x00", "before\x00after", "\ud800", "", None, 1, [], {})
        for name, operation in operations.items():
            for identity in malformed:
                with self.subTest(operation=name, identity=repr(identity)):
                    with self.assertRaises(CoreError) as error:
                        operation(identity)
                    self.assertEqual(error.exception.code, "REMOTE_AGENT_INVALID")
            for identity in ("unknown", "é", "x" * 5000):
                with self.subTest(operation=name, unknown=identity[:20]):
                    with self.assertRaises(CoreError) as error:
                        operation(identity)
                    self.assertEqual(error.exception.code, "REMOTE_AGENT_NOT_FOUND")
        for identity in malformed:
            if identity is None:
                continue  # None means the first page.
            with self.subTest(cursor=repr(identity)):
                with self.assertRaises(CoreError) as error:
                    self.store.list(self.tenant, after_id=identity)
                self.assertEqual(error.exception.code, "REQUEST_INVALID")
        self.assertEqual(self.store.list(self.tenant), [peer])

    def test_concurrent_cas_has_one_winner(self):
        peer = self.create()
        barrier = threading.Barrier(3)
        outcomes = []

        def update():
            barrier.wait(timeout=5)
            try:
                outcomes.append(self.update(peer)["revision"])
            except CoreError as error:
                outcomes.append(error.code)

        threads = [threading.Thread(target=update) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(outcomes, [2, "REMOTE_AGENT_CONFLICT"])

    def test_invalid_revision_and_missing_key_mutations_leave_previous_revision(self):
        peer = self.create(header_value="SECRET")
        for revision in (True, False, "1", 0, -1, 1.5):
            with self.subTest(revision=revision):
                with self.assertRaises(CoreError) as error:
                    self.store.get_revision(self.tenant, peer["id"], revision)
                self.assertEqual(error.exception.code, "REMOTE_AGENT_INVALID")
        original = self.store._cipher
        self.store._cipher = None
        self.assertEqual(self.store.get_revision(self.tenant, peer["id"]), peer)
        with self.assertRaises(CoreError) as error:
            self.update(peer, description="changed")
        self.assertEqual(error.exception.code, "REMOTE_AGENT_CREDENTIAL_UNAVAILABLE")
        self.assertEqual(self.store.get_revision(self.tenant, peer["id"]), peer)
        cleared = self.update(peer, header_value=None)
        self.assertFalse(cleared["has_header_value"])
        self.assertEqual(self.store.resolve_headers(self.tenant, peer["id"], 2), {})
        self.store._cipher = original
        self.assertEqual(self.store.resolve_headers(self.tenant, peer["id"], 1), {"Authorization": "SECRET"})


class MemoryRemoteRegistryTests(RemoteRegistryContract, unittest.TestCase):
    def setUp(self):
        self.tenant = str(uuid.uuid4())
        self.key = Fernet.generate_key()
        self.store = InMemoryRemoteRegistry(self.key)

    def raw(self, peer_id, revision):
        return dict(self.store._revisions[(self.tenant, peer_id, revision)])

    def tamper(self, peer_id, revision, field, value):
        self.store._revisions[(self.tenant, peer_id, revision)][field] = value

    def test_ephemeral_key_wrong_key_unknown_version_and_company_binding(self):
        ephemeral = InMemoryRemoteRegistry()
        peer = ephemeral.create(self.tenant, self.values(header_value="SECRET"), actor_id="owner")
        self.assertEqual(ephemeral.resolve_headers(self.tenant, peer["id"], 1), {"Authorization": "SECRET"})
        peer = self.create(header_value="SECRET")
        self.store._cipher = Fernet(Fernet.generate_key())
        with self.assertRaises(CoreError) as error:
            self.store.resolve_headers(self.tenant, peer["id"], 1)
        self.assertEqual(error.exception.code, "REMOTE_AGENT_CREDENTIAL_UNAVAILABLE")
        self.store._cipher = Fernet(self.key)
        self.tamper(peer["id"], 1, "tenant_id", "other")
        with self.assertRaises(CoreError) as error:
            self.store.resolve_headers(self.tenant, peer["id"], 1)
        self.assertEqual(error.exception.code, "REMOTE_AGENT_CREDENTIAL_UNAVAILABLE")
        self.tamper(peer["id"], 1, "storage_version", 2)
        with self.assertRaises(CoreError) as error:
            self.store.get_revision(self.tenant, peer["id"], 1)
        self.assertEqual(error.exception.code, "CHECKPOINT_INVALID")
        with self.assertRaises(CoreError) as error:
            InMemoryRemoteRegistry("not-a-key")
        self.assertEqual(error.exception.code, "REMOTE_AGENT_CREDENTIAL_UNAVAILABLE")


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run PostgreSQL registry tests")
class PostgresRemoteRegistryTests(RemoteRegistryContract, unittest.TestCase):
    def setUp(self):
        self.tenant = str(uuid.uuid4())
        self.key = Fernet.generate_key()
        self.database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=5)
        self.addCleanup(self.database.close)
        self.database.migrate()
        self.store = PostgresRemoteRegistry(self.database, self.key)

    def raw(self, peer_id, revision):
        with self.database.pool.connection() as connection:
            return connection.execute("SELECT * FROM core_remote_agent_revisions WHERE tenant_id = %s AND id = %s AND revision = %s",
                                      (self.tenant, peer_id, revision)).fetchone()

    def tamper(self, peer_id, revision, field, value):
        from psycopg.sql import SQL, Identifier

        with self.database.transaction() as connection:
            connection.execute(SQL("UPDATE core_remote_agent_revisions SET {} = %s WHERE tenant_id = %s AND id = %s AND revision = %s").format(Identifier(field)),
                               (value, self.tenant, peer_id, revision))

    def test_restart_missing_key_and_borrowed_connection(self):
        peer = self.create(header_value="SECRET")
        fresh = PostgresRemoteRegistry(self.database, self.key)
        with self.database.transaction() as connection:
            self.assertEqual(fresh.get_revision(self.tenant, peer["id"], 1, connection=connection), peer)
            self.assertEqual(fresh.resolve_headers(self.tenant, peer["id"], 1, connection=connection), {"Authorization": "SECRET"})
        unavailable = PostgresRemoteRegistry(self.database)
        self.assertEqual(unavailable.list(self.tenant), [peer])
        with self.assertRaises(CoreError) as error:
            unavailable.resolve_headers(self.tenant, peer["id"], 1)
        self.assertEqual(error.exception.code, "REMOTE_AGENT_CREDENTIAL_UNAVAILABLE")
        with self.assertRaises(CoreError):
            unavailable.create(self.tenant, self.values(name="second", header_value="SECRET"), actor_id="owner")
        plain = unavailable.create(self.tenant, self.values(name="plain"), actor_id="owner")
        self.assertEqual(unavailable.resolve_headers(self.tenant, plain["id"], 1), {})
        with self.assertRaises(CoreError):
            unavailable.disable(self.tenant, peer["id"], expected_revision=1, actor_id="owner")
        self.assertEqual(fresh.get_revision(self.tenant, peer["id"]), peer)
        wrong = PostgresRemoteRegistry(self.database, Fernet.generate_key())
        with self.assertRaises(CoreError) as error:
            wrong.resolve_headers(self.tenant, peer["id"], 1)
        self.assertEqual(error.exception.code, "REMOTE_AGENT_CREDENTIAL_UNAVAILABLE")

