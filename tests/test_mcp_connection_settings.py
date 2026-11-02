"""Regression proof for per-connection MCP proxy and pinned mTLS identity.

Covers the owner write semantics (preserve/replace/clear), the bounded and safe
validation of uploaded identity material, the absence of any secret in the
public projection and the admission snapshot, version1 header-only compatibility
next to the version2 envelope, and hydration of proxy credentials plus client
identity from the pinned company revision for root, child and recovery.
"""

import base64
import copy
import datetime
import json
import types
import unittest

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from core_agent.agent_settings import AgentSettingsStore
from core_agent.config import PlatformConfig
from core_agent.errors import CoreError
from core_agent.mcp_credentials import (
    normalize_client_certificate,
    validate_proxy_credentials,
    validate_proxy_url,
    validate_stored_client_certificate,
    validate_stored_proxy_credentials,
)
from core_agent.workflow import InMemoryWorkflowStore
from tests.test_auth import AuthAppTestCase

TENANT = "mcp-settings-tenant"
PROXY = "http://proxy.test:3128"
LEAF_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _certificate(key, subject, *, issuer_key=None, issuer="mcp-test-ca", days_before=1, days_after=365):
    now = datetime.datetime.now(datetime.timezone.utc)
    name = lambda value: x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, value)])  # noqa: E731
    return (
        x509.CertificateBuilder()
        .subject_name(name(subject))
        .issuer_name(name(issuer))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=days_before))
        .not_valid_after(now + datetime.timedelta(days=days_after))
        .sign(issuer_key or CA_KEY, SHA256())
    )


LEAF = _certificate(LEAF_KEY, "mcp-client")
INTERMEDIATE = _certificate(CA_KEY, "mcp-int")
EXPIRED = _certificate(LEAF_KEY, "mcp-client", days_before=400, days_after=-30)
FUTURE = _certificate(LEAF_KEY, "mcp-client", days_before=-30, days_after=300)


def _certificate_pem(*certificates):
    return "".join(item.public_bytes(Encoding.PEM).decode("ascii") for item in certificates)


def _key_pem(key, *, passphrase=None):
    algorithm = BestAvailableEncryption(passphrase.encode()) if passphrase else NoEncryption()
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, algorithm).decode("ascii")


def pem_upload(*, key=LEAF_KEY, certificates=(LEAF,), passphrase=None):
    certificate_pem = _certificate_pem(*certificates)
    upload = {"format": "pem", "certificate": certificate_pem, "private_key": _key_pem(key, passphrase=passphrase)}
    if passphrase:
        upload["password"] = passphrase
    return upload, certificate_pem, _key_pem(key)


def pkcs12_upload(*, key=LEAF_KEY, certificate=LEAF, chain=(INTERMEDIATE,), passphrase="upload-passphrase"):
    data = pkcs12.serialize_key_and_certificates(
        b"mcp-client", key, certificate, list(chain),
        BestAvailableEncryption(passphrase.encode()) if passphrase else NoEncryption())
    upload = {"format": "pkcs12", "data": base64.b64encode(data).decode("ascii")}
    if passphrase:
        upload["password"] = passphrase
    return upload, _certificate_pem(certificate, *chain), _key_pem(key)


def _platform():
    return PlatformConfig(set(), set(), set(), {}, set(), {"mcp"})


class McpProxySettingsTests(unittest.TestCase):
    def setUp(self):
        self.store = AgentSettingsStore(InMemoryWorkflowStore(), Fernet.generate_key().decode())

    def put(self, servers, expected_revision):
        return self.store.update(TENANT, {"mcp_servers": servers},
                                 expected_revision=expected_revision, actor_id="owner")

    def server(self, **values):
        item = {"name": "owner_docs", "url": "https://docs.test/mcp", "enabled": True, "header_name": ""}
        item.update(values)
        return item

    def current(self):
        return self.store.get(TENANT)

    def assert_rejected(self, servers, revision):
        with self.assertRaises(CoreError) as caught:
            self.put(servers, revision)
        self.assertEqual(caught.exception.code, "SETTINGS_INVALID")

    def test_proxy_address_preserves_replaces_and_clears_with_its_credentials(self):
        row = self.put([self.server(proxy_url=PROXY, proxy_username="proxy-user",
                                    proxy_password="proxy-pass")], 0)
        self.assertEqual(row["config"]["mcp_servers"][0]["proxy_url"], PROXY)
        self.assertTrue(row["config"]["mcp_servers"][0]["has_proxy_credentials"])
        self.assertEqual(self.store._secrets(row)["owner_docs"]["proxy_auth"],
                         {"username": "proxy-user", "password": "proxy-pass"})
        preserved = self.put([self.server()], 1)
        self.assertEqual(preserved["config"]["mcp_servers"][0]["proxy_url"], PROXY)
        self.assertEqual(self.store._secrets(preserved)["owner_docs"]["proxy_auth"],
                         {"username": "proxy-user", "password": "proxy-pass"})
        replaced = self.put([self.server(proxy_url="https://other-proxy.test", proxy_username="second",
                                         proxy_password="second-pass")], 2)
        self.assertEqual(replaced["config"]["mcp_servers"][0]["proxy_url"], "https://other-proxy.test")
        self.assertEqual(self.store._secrets(replaced)["owner_docs"]["proxy_auth"],
                         {"username": "second", "password": "second-pass"})
        cleared = self.put([self.server(proxy_url=None)], 3)
        self.assertIsNone(cleared["config"]["mcp_servers"][0]["proxy_url"])
        self.assertFalse(cleared["config"]["mcp_servers"][0]["has_proxy_credentials"])
        self.assertNotIn("proxy_auth", self.store._secrets(cleared).get("owner_docs", {}))
        self.assertEqual(cleared["storage_version"], 1)

    def test_proxy_credentials_are_a_write_only_pair(self):
        self.assert_rejected([self.server(proxy_url=PROXY, proxy_username="only-user")], 0)
        self.assert_rejected([self.server(proxy_url=PROXY, proxy_password="only-pass")], 0)
        self.assert_rejected([self.server(proxy_url=PROXY, proxy_username="user",
                                          proxy_password=None)], 0)
        self.assert_rejected([self.server(proxy_url=PROXY, proxy_username="user\nX",
                                          proxy_password="pass")], 0)
        self.assert_rejected([self.server(proxy_url=PROXY, proxy_username="user:x",
                                          proxy_password="pass")], 0)
        self.assertEqual(self.current()["revision"], 0)
        # An empty password is a real value: HTTP Basic allows it while the username is valid.
        accepted = self.put([self.server(proxy_url=PROXY, proxy_username="user", proxy_password="")], 0)
        self.assertEqual(self.store._secrets(accepted)["owner_docs"]["proxy_auth"],
                         {"username": "user", "password": ""})

    def test_inherited_credentials_require_an_actual_proxy(self):
        self.store._deployment_mcp = ({"name": "owner_docs",
            "transport": {"type": "streamable_http", "url": "https://docs.test/mcp"}},)
        server = self.server(proxy_username="proxy-user", proxy_password="")
        self.assert_rejected([server], 0)
        self.store._deployment_mcp[0]["transport"]["proxy_url"] = PROXY
        row = self.put([server], 0)
        self.assertEqual(row["config"]["mcp_servers"][0]["proxy_url"], PROXY)

    def test_proxy_address_validation_is_bounded_and_credential_free(self):
        for candidate in ("http://user:pass@proxy.test", "http://proxy.test:3128/p", "http://proxy.test?x=1",
                          "http://proxy.test#frag", "socks5://proxy.test", "https://", "http://proxy.test:99999",
                          "http://proxy.test /", "", "http://proxy.test/" + "x" * 4096, PROXY + "/tunnel"):
            with self.assertRaises(CoreError) as caught:
                validate_proxy_url(candidate)
            self.assertEqual(caught.exception.code, "SETTINGS_INVALID")
        self.assertEqual(validate_proxy_url("http://proxy.test"), "http://proxy.test")
        self.assertEqual(validate_proxy_url("http://proxy.test/"), "http://proxy.test/")
        self.assertEqual(validate_proxy_url("https://127.0.0.1:3128"), "https://127.0.0.1:3128")
        self.assert_rejected([self.server(proxy_url="http://user:pass@proxy.test")], 0)

    def test_changed_proxy_address_requires_explicit_credential_decision(self):
        self.put([self.server(proxy_url=PROXY, proxy_username="proxy-user", proxy_password="proxy-pass")], 0)
        self.assert_rejected([self.server(proxy_url="http://new-proxy.test")], 1)
        kept = self.put([self.server(proxy_url="http://new-proxy.test", proxy_username="fresh",
                                     proxy_password="fresh-pass")], 1)
        self.assertEqual(self.store._secrets(kept)["owner_docs"]["proxy_auth"],
                         {"username": "fresh", "password": "fresh-pass"})
        cleared = self.put([self.server(proxy_url=None)], 2)
        self.assertNotIn("proxy_auth", self.store._secrets(cleared).get("owner_docs", {}))

    def test_changed_destination_url_cannot_transplant_proxy_credentials(self):
        self.put([self.server(proxy_url=PROXY, proxy_username="proxy-user", proxy_password="proxy-pass")], 0)
        self.assert_rejected([self.server(url="https://other.test/mcp")], 1)
        replaced = self.put([self.server(url="https://other.test/mcp", proxy_url=PROXY,
                                         proxy_username="second", proxy_password="second")], 1)
        self.assertEqual(self.store._secrets(replaced)["owner_docs"]["proxy_auth"],
                         {"username": "second", "password": "second"})
        cleared = self.put([self.server(url="https://other.test/mcp", proxy_url=None)], 2)
        self.assertNotIn("proxy_auth", self.store._secrets(cleared).get("owner_docs", {}))


class McpClientCertificateTests(unittest.TestCase):
    def setUp(self):
        self.store = AgentSettingsStore(InMemoryWorkflowStore(), Fernet.generate_key().decode())

    def current(self):
        return self.store.get(TENANT)

    def put(self, servers, expected_revision):
        return self.store.update(TENANT, {"mcp_servers": servers},
                                 expected_revision=expected_revision, actor_id="owner")

    def server(self, **values):
        item = {"name": "owner_docs", "url": "https://docs.test/mcp", "enabled": True, "header_name": ""}
        item.update(values)
        return item

    def test_pem_and_pkcs12_uploads_normalize_to_the_same_trusted_identity(self):
        upload, certificate_pem, key_pem = pem_upload(certificates=(LEAF, INTERMEDIATE))
        row = self.put([self.server(client_certificate=upload)], 0)
        server = row["config"]["mcp_servers"][0]
        self.assertTrue(server["has_client_certificate"])
        self.assertEqual(self.store._secrets(row)["owner_docs"]["client_certificate"],
                         {"certificate_pem": certificate_pem, "private_key_pem": key_pem})
        self.assertEqual(server["certificate_info"]["subject"], "CN=mcp-client")
        self.assertEqual(server["certificate_info"]["issuer"], "CN=mcp-test-ca")
        self.assertEqual(server["certificate_info"]["sha256"], LEAF.fingerprint(SHA256()).hex())
        self.assertIn(_certificate_pem(INTERMEDIATE), certificate_pem)
        pack12, packed_pem, packed_key = pkcs12_upload()
        converted = self.put([self.server(client_certificate=pack12)], 1)
        self.assertEqual(self.store._secrets(converted)["owner_docs"]["client_certificate"],
                         {"certificate_pem": packed_pem, "private_key_pem": packed_key})
        self.assertEqual(converted["config"]["mcp_servers"][0]["certificate_info"], server["certificate_info"])
        for secret in (certificate_pem, key_pem, "upload-passphrase"):
            self.assertNotIn(secret, json.dumps(converted["config"]))

    def test_client_certificate_preserves_and_clears_only_by_field(self):
        upload, certificate_pem, key_pem = pem_upload()
        self.put([self.server(client_certificate=upload)], 0)
        preserved = self.put([self.server()], 1)
        self.assertEqual(self.store._secrets(preserved)["owner_docs"]["client_certificate"],
                         {"certificate_pem": certificate_pem, "private_key_pem": key_pem})
        self.assertTrue(preserved["config"]["mcp_servers"][0]["has_client_certificate"])
        self.assertIsNotNone(preserved["config"]["mcp_servers"][0]["certificate_info"])
        cleared = self.put([self.server(client_certificate=None)], 2)
        self.assertFalse(cleared["config"]["mcp_servers"][0]["has_client_certificate"])
        self.assertIsNone(cleared["config"]["mcp_servers"][0]["certificate_info"])
        self.assertNotIn("client_certificate", self.store._secrets(cleared).get("owner_docs", {}))
        self.assertEqual(cleared["storage_version"], 1)

    def test_identity_rejects_key_mismatch_expiry_and_malformed_uploads(self):
        mismatch, _, _ = pem_upload(key=OTHER_KEY)
        expired, _, _ = pem_upload(certificates=(EXPIRED,))
        future, _, _ = pem_upload(certificates=(FUTURE,))
        broken = {"format": "pem", "certificate": "not a certificate",
                  "private_key": _key_pem(LEAF_KEY)}
        unknown_format = {"format": "jks", "certificate": _certificate_pem(LEAF), "private_key": _key_pem(LEAF_KEY)}
        cases = [mismatch, expired, future, broken, unknown_format,
                 {"format": "pem", "certificate": _certificate_pem(LEAF)},
                 {"format": "pkcs12", "data": "!!!not-base64!!!"},
                 {"format": "pkcs12", "data": base64.b64encode(b"junk").decode("ascii"),
                  "password": "x"},
                 {"format": "pkcs12"}, "not-a-dict"]
        for upload in cases:
            with self.assertRaises(CoreError) as caught:
                self.put([self.server(client_certificate=upload)], 0)
            self.assertEqual(caught.exception.code, "SETTINGS_INVALID", upload)
            self.assertEqual(self.current()["revision"], 0)
        encrypted = pkcs12_upload(passphrase="a-real-passphrase")
        wrong = {**encrypted[0], "password": "wrong-passphrase"}
        with self.assertRaises(CoreError) as caught:
            self.put([self.server(client_certificate=wrong)], 0)
        self.assertEqual(caught.exception.code, "SETTINGS_INVALID")
        missing = {key: value for key, value in encrypted[0].items() if key != "password"}
        with self.assertRaises(CoreError) as caught:
            self.put([self.server(client_certificate=missing)], 0)
        self.assertEqual(caught.exception.code, "SETTINGS_INVALID")
        self.assertEqual(self.current()["revision"], 0)
        self.assertEqual(caught.exception.message, caught.exception.code)

    def test_encrypted_pem_key_loads_only_with_its_own_password(self):
        upload, certificate_pem, _ = pem_upload(passphrase="key-passphrase")
        row = self.put([self.server(client_certificate=upload)], 0)
        self.assertEqual(self.store._secrets(row)["owner_docs"]["client_certificate"]["certificate_pem"],
                         certificate_pem)
        self.assertNotIn("key-passphrase", json.dumps(self.store._secrets(row)))
        self.assertNotIn("key-passphrase", json.dumps(row["config"]))
        wrong = {**upload, "password": "wrong-key-passphrase"}
        with self.assertRaises(CoreError) as caught:
            self.put([self.server(client_certificate=wrong)], 1)
        self.assertEqual(caught.exception.code, "SETTINGS_INVALID")
        missing = {key: value for key, value in upload.items() if key != "password"}
        with self.assertRaises(CoreError) as caught:
            self.put([self.server(client_certificate=missing)], 1)
        self.assertEqual(caught.exception.code, "SETTINGS_INVALID")
        self.assertEqual(self.current()["revision"], 1)
        # The upload password is optional for an unencrypted key, so an extra one is not a secret
        # problem: the key simply does not need it.
        unencrypted, unencrypted_pem, _ = pem_upload()
        tolerated = self.put([self.server(client_certificate={**unencrypted, "password": "unused-passphrase"})], 1)
        self.assertEqual(self.store._secrets(tolerated)["owner_docs"]["client_certificate"]["certificate_pem"],
                         unencrypted_pem)

    def test_unencodable_and_oversized_material_fail_safely(self):
        for upload in ({"format": "pem", "certificate": _certificate_pem(LEAF),
                        "private_key": "\ud800" + _key_pem(LEAF_KEY)},
                       {"format": "pem", "certificate": _certificate_pem(LEAF),
                        "private_key": _key_pem(LEAF_KEY), "password": "\ud800"},
                       {"format": "pkcs12", "data": "\ud800", "password": "x"}):
            with self.assertRaises(CoreError) as caught:
                normalize_client_certificate(upload)
            self.assertEqual(caught.exception.code, "SETTINGS_INVALID")
        self.assertEqual(self.current()["revision"], 0)

    def test_upload_size_is_bounded(self):
        oversized_pem = {"format": "pem", "certificate": _certificate_pem(LEAF) + "x" * 131072,
                         "private_key": _key_pem(LEAF_KEY)}
        oversized_pack12 = {"format": "pkcs12",
                            "data": base64.b64encode(b"junk" * 40000).decode("ascii"),
                            "password": "x"}
        for upload in (oversized_pem, oversized_pack12, {"format": "pem",
                        "certificate": _certificate_pem(LEAF), "private_key": _key_pem(LEAF_KEY),
                        "password": "y" * 65536}):
            with self.assertRaises(CoreError) as caught:
                self.put([self.server(client_certificate=upload)], 0)
            self.assertEqual(caught.exception.code, "SETTINGS_INVALID")
        with self.assertRaises(CoreError):
            normalize_client_certificate(oversized_pack12)
        self.assertEqual(self.current()["revision"], 0)

    def test_destination_url_change_requires_an_explicit_identity_decision(self):
        upload, certificate_pem, key_pem = pem_upload()
        self.put([self.server(client_certificate=upload)], 0)
        self.assert_rejected([self.server(url="https://other.test/mcp")], 1)
        replaced = self.put([self.server(url="https://other.test/mcp", client_certificate=upload)], 1)
        self.assertEqual(self.store._secrets(replaced)["owner_docs"]["client_certificate"],
                         {"certificate_pem": certificate_pem, "private_key_pem": key_pem})
        cleared = self.put([self.server(url="https://third.test/mcp", client_certificate=None)], 2)
        self.assertFalse(cleared["config"]["mcp_servers"][0]["has_client_certificate"])

    def assert_rejected(self, servers, revision):
        with self.assertRaises(CoreError) as caught:
            self.put(servers, revision)
        self.assertEqual(caught.exception.code, "SETTINGS_INVALID")


class McpProxyInputRulesTests(unittest.TestCase):
    def test_proxy_username_cannot_contain_the_basic_separator(self):
        with self.assertRaises(CoreError) as caught:
            validate_proxy_credentials("proxy:user", "pass")
        self.assertEqual(caught.exception.code, "SETTINGS_INVALID")
        self.assertEqual(validate_proxy_credentials("proxy-user", ""), {"username": "proxy-user",
                                                                       "password": ""})
        for username in ("", "proxy user\n", "proxy\x00user", None):
            with self.assertRaises(CoreError):
                validate_proxy_credentials(username, "pass")
        for password in ("pass\nword", None, "\ud800"):
            with self.assertRaises(CoreError):
                validate_proxy_credentials("proxy-user", password)

    def test_proxy_address_rejects_query_and_fragment_separators(self):
        for candidate in ("http://proxy.test?", "http://proxy.test#", "http://proxy.test?a=1",
                          "http://proxy.test#frag", "https://proxy.test:3128/?", "http://proxy.test/#"):
            with self.assertRaises(CoreError) as caught:
                validate_proxy_url(candidate)
            self.assertEqual(caught.exception.code, "SETTINGS_INVALID")

    def test_stored_proxy_entry_shape_is_exact(self):
        for value in ({"username": "u"}, {"username": "u", "password": "p", "extra": 1},
                      {"username": "u", "password": None}, {"username": b"u", "password": "p"},
                      {"username": "u", "password": "p" * 5000}, "not-a-dict"):
            self.assertFalse(validate_stored_proxy_credentials(value))
        self.assertTrue(validate_stored_proxy_credentials({"username": "u", "password": ""}))
        for value in ({"certificate_pem": "x"}, {"certificate_pem": "x", "private_key_pem": "y", "z": 1},
                      {"certificate_pem": "x", "private_key_pem": None}, 7):
            self.assertFalse(validate_stored_client_certificate(value))


class McpSecretHygieneTests(unittest.TestCase):
    AGENT = types.SimpleNamespace(platform_mcp=(), agent_config=types.SimpleNamespace(
        agent={}, model={"route": "deployment-model"}), context_window=1024,
        next_steps_every=None, next_steps_default_every=3, next_steps_available=True)

    def setUp(self):
        self.store = AgentSettingsStore(InMemoryWorkflowStore(), Fernet.generate_key().decode())

    def put(self, servers, expected_revision=0):
        return self.store.update(TENANT, {"mcp_servers": servers},
                                 expected_revision=expected_revision, actor_id="owner")

    def server(self, **values):
        item = {"name": "owner_docs", "url": "https://docs.test/mcp", "enabled": True,
                "header_name": "Authorization", "header_value": "literal-token-canary"}
        item.update(values)
        return item

    def test_public_projection_and_configure_expose_no_secret_material(self):
        upload, certificate_pem, key_pem = pem_upload()
        row = self.put([self.server(proxy_url=PROXY, proxy_username="proxy-user",
                                    proxy_password="proxy-canary", client_certificate=upload)])
        public = self.store.public(row, self.AGENT)
        rendered = json.dumps(public)
        for secret in ("proxy-canary", "literal-token-canary", certificate_pem, key_pem, "proxy-user"):
            self.assertNotIn(secret, rendered)
        server = public["mcp_servers"][0]
        self.assertEqual(server["proxy_url"], PROXY)
        self.assertTrue(server["has_proxy_credentials"])
        self.assertTrue(server["has_client_certificate"])
        self.assertEqual(server["certificate_info"]["subject"], "CN=mcp-client")

        _, _, declarations = self.store.configure(row, {}, _platform(), (), profile=False, credentials=True)
        declaration = declarations[0]
        self.assertEqual(declaration["proxy_auth"], {"username": "proxy-user", "password": "proxy-canary"})
        self.assertEqual(declaration["client_certificate"],
                         {"certificate_pem": certificate_pem, "private_key_pem": key_pem})
        self.assertEqual(declaration["transport"]["proxy_url"], PROXY)
        self.assertNotIn("Bearer", declaration["headers"]["Authorization"])
        _, _, without = self.store.configure(row, {}, _platform(), (), profile=False, credentials=False)
        for key in ("headers", "proxy_auth", "client_certificate"):
            self.assertNotIn(key, without[0])
        self.assertEqual(without[0]["transport"]["proxy_url"], PROXY)
        self.assertNotIn("proxy-canary", json.dumps(without))

    def test_echoed_display_fields_cannot_forge_state(self):
        upload, _, _ = pem_upload()
        row = self.put([self.server(client_certificate=upload)])
        public = self.store.public(row, self.AGENT)["mcp_servers"][0]
        self.assertNotIn("proxy_declared", public)
        forged = self.put([{**public, "has_header_value": False,
                           "has_proxy_credentials": True, "has_client_certificate": False,
                           "certificate_info": {"subject": "CN=attacker"}}], 1)
        server = forged["config"]["mcp_servers"][0]
        self.assertTrue(server["has_client_certificate"])
        self.assertEqual(server["certificate_info"]["subject"], "CN=mcp-client")
        self.assertFalse(server["has_proxy_credentials"])
        self.assertIsNone(server["proxy_url"])
        self.assertEqual(self.store._secrets(forged)["owner_docs"]["header_value"], "literal-token-canary")
        with self.assertRaises(CoreError) as caught:
            self.put([{**public, "unknown_field": True}], 2)
        self.assertEqual(caught.exception.code, "SETTINGS_INVALID")

    def test_malformed_envelopes_are_unavailable_not_partially_trusted(self):
        upload, _, _ = pem_upload()
        row = self.put([self.server(proxy_url=PROXY, proxy_username="proxy-user",
                                    proxy_password="proxy-pass", client_certificate=upload)])
        envelope = json.loads(self.store._cipher.decrypt(bytes(row["encrypted_payload"])))

        def seal(content):
            tampered = copy.deepcopy(envelope)
            content(tampered)
            row["encrypted_payload"] = self.store._cipher.encrypt(
                json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode())
            with self.assertRaises(CoreError) as caught:
                self.store._secrets(row)
            self.assertEqual(caught.exception.code, "SETTINGS_CREDENTIAL_UNAVAILABLE")

        for content in (
            lambda item: item["servers"]["owner_docs"].pop("header_name"),
            lambda item: item["servers"]["owner_docs"].update({"header_name": "Authorization",
                                                               "header_value": "literal-token-canary",
                                                               "unexpected": 1}),
            lambda item: item["servers"]["owner_docs"]["proxy_auth"].update({"extra": "x"}),
            lambda item: item["servers"]["owner_docs"]["proxy_auth"].pop("password"),
            lambda item: item["servers"]["owner_docs"]["proxy_auth"].update({"username": 7}),
            lambda item: item["servers"]["owner_docs"]["client_certificate"].pop("private_key_pem"),
            lambda item: item["servers"]["owner_docs"]["client_certificate"].update({"password": "x"}),
            lambda item: item["servers"].update({"other": {"header_value": "x"}}),
            lambda item: item["servers"]["owner_docs"].update({"header_name": "X-Other"}),
            lambda item: item.update({"revision": 99}),
            lambda item: item.pop("revision"),
            lambda item: item.update({"version": 3}),
        ):
            seal(content)
        # A version1 envelope must carry exactly the header pair per connection.
        legacy = {"version": 1, "tenant_id": TENANT, "revision": row["revision"],
                  "headers": {"owner_docs": {"header_value": "literal-token-canary"}}}
        for entry in ({"header_value": "x"}, {"header_name": "Authorization"},
                      {"header_name": "Authorization", "header_value": "x", "proxy_auth": {
                          "username": "u", "password": "p"}}):
            legacy["headers"]["owner_docs"] = entry
            row["encrypted_payload"] = self.store._cipher.encrypt(
                json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode())
            with self.assertRaises(CoreError) as caught:
                self.store._secrets(row)
            self.assertEqual(caught.exception.code, "SETTINGS_CREDENTIAL_UNAVAILABLE")
        legacy["headers"]["owner_docs"] = {"header_name": "Authorization",
                                           "header_value": "literal-token-canary"}
        row["encrypted_payload"] = self.store._cipher.encrypt(
            json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode())
        self.assertEqual(self.store._secrets(row), {"owner_docs": {
            "header_name": "Authorization", "header_value": "literal-token-canary"}})

    def test_disabled_connection_does_not_load_identity(self):
        upload, _, _ = pem_upload()
        row = self.put([self.server(proxy_url=PROXY, proxy_username="proxy-user",
                                    proxy_password="proxy-canary", client_certificate=upload)])
        _, _, declarations = self.store.configure(row, {}, _platform(), (), profile=False, credentials=True)
        self.assertEqual(len(declarations), 1)
        disabled = self.put([self.server(proxy_url=PROXY, proxy_username="proxy-user",
                                         proxy_password="proxy-canary", client_certificate=upload,
                                         enabled=False)], 1)
        _, _, declarations = self.store.configure(disabled, {}, _platform(), (), profile=False, credentials=True)
        self.assertEqual(declarations, ())

    def test_version1_envelope_stays_readable_and_new_writes_use_bound_version2(self):
        upload, certificate_pem, key_pem = pem_upload()
        row = self.put([self.server(proxy_url=PROXY, proxy_username="proxy-user",
                                    proxy_password="proxy-canary", client_certificate=upload)])
        envelope = json.loads(self.store._cipher.decrypt(bytes(row["encrypted_payload"])))
        self.assertEqual(envelope["version"], 2)
        self.assertEqual(envelope["tenant_id"], TENANT)
        self.assertEqual(envelope["revision"], row["revision"])
        self.assertEqual(envelope["servers"]["owner_docs"]["header_name"], "Authorization")
        legacy = copy.deepcopy(row)
        legacy["config"]["mcp_servers"] = [{key: value for key, value in item.items()
                                            if key not in {"proxy_url", "has_proxy_credentials",
                                                           "has_client_certificate", "certificate_info"}}
                                           for item in legacy["config"]["mcp_servers"]]
        legacy["encrypted_payload"] = self.store._cipher.encrypt(json.dumps({
            "version": 1, "tenant_id": TENANT, "revision": row["revision"],
            "headers": {"owner_docs": {"header_name": "Authorization", "header_value": "literal-token-canary"}},
        }, sort_keys=True, separators=(",", ":")).encode())
        self.store._revisions[(TENANT, legacy["revision"])] = legacy
        self.store._current[TENANT] = legacy["revision"]
        self.assertEqual(self.store._secrets(legacy),
                         {"owner_docs": {"header_name": "Authorization",
                                         "header_value": "literal-token-canary"}})
        _, _, declarations = self.store.configure(legacy, {}, _platform(), (), profile=False, credentials=True)
        self.assertEqual(declarations[0]["headers"], {"Authorization": "literal-token-canary"})
        for key in ("proxy_auth", "client_certificate"):
            self.assertNotIn(key, declarations[0])
        self.assertEqual(legacy["storage_version"], 1)
        self.assertEqual(self.store.public(legacy, self.AGENT)["mcp_servers"][0]["proxy_url"], None)
        tampered = json.loads(json.dumps(envelope))
        tampered["tenant_id"] = "other-tenant"
        row["encrypted_payload"] = self.store._cipher.encrypt(
            json.dumps(tampered, sort_keys=True).encode())
        with self.assertRaises(CoreError) as caught:
            self.store._secrets(row)
        self.assertEqual(caught.exception.code, "SETTINGS_CREDENTIAL_UNAVAILABLE")
        tampered = json.loads(json.dumps(envelope))
        tampered["servers"]["owner_docs"]["proxy_auth"] = {"username": "x"}
        row["encrypted_payload"] = self.store._cipher.encrypt(
            json.dumps(tampered, sort_keys=True).encode())
        with self.assertRaises(CoreError) as caught:
            self.store._secrets(row)
        self.assertEqual(caught.exception.code, "SETTINGS_CREDENTIAL_UNAVAILABLE")
        self.assertEqual(self.store.get(TENANT)["storage_version"], 1)


class McpPinnedIdentityRuntimeTests(AuthAppTestCase):
    automatic_tools = False
    push_encryption_key = "gLgFI-gLRDHqUBOd7HGWKfdBLZuYX-m3t_k78ohw0Aw="

    async def put(self, servers, expected_revision):
        result = await self.http.put("/api/agent-settings", headers=self.headers("owner-a"),
                                     json={"expected_revision": expected_revision, "mcp_servers": servers})
        self.assertEqual(result.status_code, 200, result.text)
        return result

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tenant = self.app.state.authenticator.settings.tenant
        self.upload, self.certificate_pem, self.key_pem = pem_upload()

    def server(self, **values):
        item = {"name": "owner_docs", "url": "https://docs.test/mcp", "enabled": True, "header_name": ""}
        item.update(values)
        return item

    async def test_snapshot_is_clean_and_pinned_revision_hydrates_identity(self):
        agent = self.app.state.core_agent
        revision = 0
        created = await self.put([self.server(proxy_url=PROXY, proxy_username="proxy-user",
                                              proxy_password="proxy-canary",
                                              client_certificate=self.upload)], revision)
        revision += 1
        rendered = created.text
        for secret in ("proxy-canary", self.certificate_pem, self.key_pem):
            self.assertNotIn(secret, rendered)
        server = created.json()["mcp_servers"][0]
        self.assertEqual(server["proxy_url"], PROXY)
        self.assertTrue(server["has_proxy_credentials"], server)
        self.assertTrue(server["has_client_certificate"])
        self.assertEqual(server["certificate_info"]["subject"], "CN=mcp-client")

        record, *_ = agent._new_workflow({"prompt": "inspect"}, task_id="pinned-identity-task",
                                         identity="owner", session_id="pinned-identity-chat",
                                         tenant_id=self.tenant, defer_initialization=True)
        snapshot = json.dumps(record.snapshot)
        for secret in ("proxy-canary", self.certificate_pem, self.key_pem):
            self.assertNotIn(secret, snapshot)
        admitted = record.snapshot["admission"]["mcp"][0]
        self.assertEqual(admitted["transport"]["proxy_url"], PROXY)
        for key in ("proxy_auth", "client_certificate"):
            self.assertNotIn(key, admitted)

        identity = {"username": "proxy-user", "password": "proxy-canary"}
        _, _, _, declarations, _ = agent._admission_inputs(record)
        self.assertEqual(declarations[0]["proxy_auth"], identity)
        self.assertEqual(declarations[0]["client_certificate"],
                         {"certificate_pem": self.certificate_pem, "private_key_pem": self.key_pem})

        raw = record.snapshot["admission"]["agent_config"]
        child = agent._child_agent({**copy.deepcopy(raw), "tools": {**raw["tools"], "mcp": {
            "default": "deny", "allow_servers": ["owner_docs"], "allow_tools": {"owner_docs": ["read"]}}}}, ())
        self.addCleanup(child.close)
        child_record, *_ = child._new_workflow({"prompt": "read only"}, task_id="pinned-identity-child",
                                               identity=record.owner_id, session_id=record.context_id,
                                               tenant_id=record.tenant_id, parent_run_id=record.run_id,
                                               defer_initialization=True)
        self.assertNotIn("proxy-canary", json.dumps(child_record.snapshot))
        child_declarations = child._admission_inputs(child_record)[3]
        self.assertEqual(child_declarations[0]["proxy_auth"], identity)
        self.assertEqual(child_declarations[0]["client_certificate"], declarations[0]["client_certificate"])

        # A second runtime over the same stores proves recovery reads the pinned row, not live settings.
        restarted = agent._child_agent(copy.deepcopy(agent.agent_config.to_dict()), ())
        self.addCleanup(restarted.close)
        recovery_declarations = restarted._admission_inputs(record)[3]
        self.assertEqual(recovery_declarations[0]["proxy_auth"], identity)
        self.assertEqual(recovery_declarations[0]["client_certificate"],
                         declarations[0]["client_certificate"])

        replacement, replacement_pem, replacement_key = pem_upload(key=OTHER_KEY, certificates=(
            _certificate(OTHER_KEY, "rotated-client"),))
        await self.put([self.server(proxy_url="http://other-proxy.test:8080", proxy_username="rotated",
                                    proxy_password="rotated-canary", client_certificate=replacement)], revision)
        self.assertEqual(restarted._admission_inputs(record)[3][0]["proxy_auth"], identity)
        self.assertNotIn("rotated-canary", json.dumps(agent._admission_inputs(record)[3]))
        latest, *_ = agent._new_workflow({"prompt": "after rotation"}, task_id="rotated-identity-task",
                                         identity="owner", session_id="rotated-identity-chat",
                                         tenant_id=self.tenant, defer_initialization=True)
        rotated = agent._admission_inputs(latest)[3][0]
        self.assertEqual(rotated["proxy_auth"], {"username": "rotated", "password": "rotated-canary"})
        self.assertEqual(rotated["client_certificate"],
                         {"certificate_pem": replacement_pem, "private_key_pem": replacement_key})

    async def test_records_without_their_own_declaration_never_gain_identity(self):
        agent = self.app.state.core_agent
        before, *_ = agent._new_workflow({"prompt": "before"}, task_id="identity-before-task",
                                         identity="owner", session_id="identity-before-chat",
                                         tenant_id=self.tenant, defer_initialization=True)
        self.assertEqual(agent._admission_inputs(before)[3], tuple(agent.platform_mcp))
        await self.put([self.server(proxy_url=PROXY, proxy_username="proxy-user",
                                    proxy_password="proxy-canary",
                                    client_certificate=self.upload)], 0)
        self.assertEqual(agent._admission_inputs(before)[3], tuple(agent.platform_mcp))
        await self.put([self.server(proxy_url=PROXY, proxy_username="proxy-user",
                                    proxy_password="proxy-canary",
                                    client_certificate=self.upload, enabled=False)], 1)
        disabled, *_ = agent._new_workflow({"prompt": "disabled"}, task_id="identity-disabled-task",
                                           identity="owner", session_id="identity-disabled-chat",
                                           tenant_id=self.tenant, defer_initialization=True)
        self.assertEqual(agent._admission_inputs(disabled)[3], ())

    async def test_inherited_deployment_connection_carries_company_transport_settings(self):
        agent = self.app.state.core_agent
        declaration = {"name": "legacy_docs", "required": False, "read_only_tools": ["search"],
                       "transport": {"type": "streamable_http", "url": "http://legacy-user:legacy-secret@legacy.test/mcp",
                                     "proxy_url": "http://deployment-proxy.test:3128"},
                       "headers": {"X-Deployment": "deployment-header-canary"}}
        plain = {"name": "legacy_plain", "required": False,
                 "transport": {"type": "streamable_http", "url": "https://plain.test/mcp"}}
        agent.platform_mcp = (declaration, plain)
        agent.agent_settings_store._deployment_mcp = agent.platform_mcp
        server = {"name": "legacy_docs", "url": "http://legacy.test/mcp", "enabled": True, "header_name": "",
                  "proxy_url": "http://owner-proxy.test:8080", "proxy_username": "owner-proxy-user",
                  "proxy_password": "owner-proxy-canary", "client_certificate": self.upload}
        # The second link is a deployment connection that never had headers of its own.
        proxy_only = {"name": "legacy_plain", "url": "https://plain.test/mcp", "enabled": True,
                      "header_name": "", "proxy_url": "http://owner-proxy.test:8080",
                      "proxy_username": "plain-proxy-user", "proxy_password": "plain-canary"}
        created = await self.put([server, proxy_only], 0)
        self.assertNotIn("owner-proxy-canary", created.text)
        self.assertNotIn("deployment-header-canary", created.text)
        self.assertTrue(created.json()["mcp_servers"][0]["has_client_certificate"])

        record, *_ = agent._new_workflow({"prompt": "inspect"}, task_id="inherited-identity-task",
                                         identity="owner", session_id="inherited-identity-chat",
                                         tenant_id=self.tenant, defer_initialization=True)
        snapshot = record.snapshot["admission"]["mcp"][0]
        self.assertNotIn("owner_configured", snapshot)
        self.assertTrue(snapshot["owner_transport_configured"])
        self.assertEqual(snapshot["transport"]["proxy_url"], "http://owner-proxy.test:8080")
        self.assertEqual(snapshot["headers"], {"X-Deployment": "deployment-header-canary"})
        rendered = json.dumps(record.snapshot)
        for secret in ("owner-proxy-canary", "owner-proxy-user", self.certificate_pem, self.key_pem):
            self.assertNotIn(secret, rendered)

        identity = {"username": "owner-proxy-user", "password": "owner-proxy-canary"}
        declarations = agent._admission_inputs(record)[3]
        headerless = next(item for item in declarations if item["name"] == "legacy_plain")
        self.assertTrue(headerless["owner_transport_configured"])
        self.assertNotIn("headers", headerless)
        self.assertEqual(headerless["proxy_auth"], {"username": "plain-proxy-user", "password": "plain-canary"})
        self.assertNotIn("owner_configured", headerless)
        self.assertNotIn("plain-canary", json.dumps(record.snapshot))
        self.assertEqual(declarations[0]["proxy_auth"], identity)
        self.assertEqual(declarations[0]["client_certificate"],
                         {"certificate_pem": self.certificate_pem, "private_key_pem": self.key_pem})
        self.assertEqual(declarations[0]["headers"], {"X-Deployment": "deployment-header-canary"})
        self.assertNotIn("owner_configured", declarations[0])
        # Deployment tool allow semantics stay with the deployment: the marker must not add the
        # connection to the owner-managed server list.
        configured_raw, _, _ = agent.agent_settings_store.configure(
            agent.agent_settings_store.get(self.tenant, record.snapshot["company_settings_revision"]),
            copy.deepcopy(agent.agent_config.to_dict()), agent.platform_config, agent.platform_mcp)
        self.assertIn("legacy_docs", configured_raw["tools"]["mcp"]["allow_servers"])
        self.assertNotIn("legacy_docs", configured_raw["tools"]["mcp"]["owner_servers"])

        child = agent._child_agent(copy.deepcopy(agent._admission_inputs(record)[0]), ())
        self.addCleanup(child.close)
        child_record, *_ = child._new_workflow({"prompt": "read"}, task_id="inherited-identity-child",
                                               identity=record.owner_id, session_id=record.context_id,
                                               tenant_id=record.tenant_id, parent_run_id=record.run_id,
                                               defer_initialization=True)
        self.assertNotIn("owner-proxy-canary", json.dumps(child_record.snapshot))
        child_declarations = child._admission_inputs(child_record)[3]
        self.assertEqual(child_declarations[0]["proxy_auth"], identity)
        self.assertEqual(child_declarations[0]["client_certificate"], declarations[0]["client_certificate"])

        restarted = agent._child_agent(copy.deepcopy(agent.agent_config.to_dict()), ())
        self.addCleanup(restarted.close)
        recovery = restarted._admission_inputs(record)[3]
        self.assertEqual(recovery[0]["proxy_auth"], identity)
        self.assertEqual(recovery[0]["client_certificate"], declarations[0]["client_certificate"])

        # Later revisions do not move the pinned identity, and an explicit clear wins over the
        # deployment address only for records admitted afterwards.
        await self.put([{**server, "proxy_url": None, "proxy_username": None, "proxy_password": None,
                         "client_certificate": None}], 1)
        self.assertEqual(restarted._admission_inputs(record)[3][0]["proxy_auth"], identity)
        self.assertEqual(agent._admission_inputs(record)[3][0]["proxy_auth"], identity)
        cleared, *_ = agent._new_workflow({"prompt": "cleared"}, task_id="inherited-cleared-task",
                                          identity="owner", session_id="inherited-cleared-chat",
                                          tenant_id=self.tenant, defer_initialization=True)
        cleared_declaration = agent._admission_inputs(cleared)[3][0]
        self.assertNotIn("proxy_auth", cleared_declaration)
        self.assertNotIn("client_certificate", cleared_declaration)
        self.assertNotIn("proxy_url", cleared_declaration["transport"])
        self.assertNotIn("owner_transport_configured", cleared_declaration)
        rejected = await self.http.put("/api/agent-settings", headers=self.headers("owner-a"),
                                       json={"expected_revision": 2, "mcp_servers": [
                                           {**server, "url": "http://other.test/mcp"}]})
        self.assertEqual(rejected.status_code, 400)

    async def test_owner_read_never_returns_secret_material(self):
        agent = self.app.state.core_agent
        await self.put([self.server(proxy_url=PROXY, proxy_username="proxy-user",
                                    proxy_password="proxy-canary",
                                    client_certificate=self.upload)], 0)
        read = await self.http.get("/api/agent-settings", headers=self.headers("owner-a"))
        self.assertEqual(read.status_code, 200, read.text)
        for secret in ("proxy-canary", "proxy-user", self.certificate_pem, self.key_pem):
            self.assertNotIn(secret, read.text)
        self.assertEqual(read.json()["mcp_servers"][0]["certificate_info"]["sha256"],
                         LEAF.fingerprint(SHA256()).hex())
        rejected = await self.http.put("/api/agent-settings", headers=self.headers("owner-a"),
                                       json={"expected_revision": 1, "mcp_servers": [
                                           self.server(proxy_username="half", proxy_password=None)]})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(agent.agent_settings_store.get(self.tenant)["revision"], 1)
        self.assertEqual(agent.agent_settings_store.get(self.tenant)["storage_version"], 1)


if __name__ == "__main__":
    unittest.main()
