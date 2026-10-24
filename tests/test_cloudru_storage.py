"""Explicit storage activation and bounded, credential-safe deployment configuration."""
import os
import unittest
from unittest.mock import patch

from core_agent.app import _workspace_storage
from core_agent.errors import CoreError
from core_agent.s3_storage import S3BlobStore


class CloudStorageConfigurationTests(unittest.TestCase):
    def environment(self):
        return {
            "CORE_AGENT_AUTH_MODE": "cloudru", "CORE_AGENT_ENVIRONMENT": "development",
            "CORE_AGENT_TENANT_ID": "company", "WORKSPACE_STORAGE_TYPE": "s3",
            "ARTIFACT_S3_ENDPOINT_URL": "https://s3.example.test", "ARTIFACT_S3_BUCKET": "workspace",
            "ARTIFACT_S3_REGION": "test", "ARTIFACT_S3_ACCESS_KEY_ID": "test-key",
            "ARTIFACT_S3_SECRET_ACCESS_KEY": "private-test-secret",
        }

    def test_platform_fields_do_not_enable_s3(self):
        values = self.environment()
        values.pop("WORKSPACE_STORAGE_TYPE")
        with patch.dict(os.environ, values, clear=True):
            self.assertIsNone(_workspace_storage())

    def test_explicit_s3_uses_platform_credentials_and_workspace_override(self):
        with patch.dict(os.environ, {**self.environment(), "WORKSPACE_S3_BUCKET": "override"}, clear=True):
            settings = _workspace_storage()
        self.assertEqual(settings["bucket"], "override")
        self.assertEqual(settings["secret_access_key"], "private-test-secret")
        self.assertEqual(settings["tenant_id"], "company")
        self.assertEqual((settings["max_bytes"], settings["max_files"], settings["max_inflight"]),
                         (100_000_000, 10_000, 4))

    def test_production_requires_explicit_complete_s3_configuration(self):
        values = {**self.environment(), "CORE_AGENT_ENVIRONMENT": "production"}
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(_workspace_storage()["tenant_id"], "company")
        for override in ({"WORKSPACE_STORAGE_TYPE": ""}, {"WORKSPACE_STORAGE_TYPE": "posix"},
                         {"ARTIFACT_S3_SECRET_ACCESS_KEY": ""}, {"ARTIFACT_S3_BUCKET": ""}):
            with self.subTest(override=override), patch.dict(os.environ, {**values, **override}, clear=True):
                with self.assertRaises(CoreError) as caught:
                    _workspace_storage()
                self.assertEqual(caught.exception.code, "CONFIG_INVALID")
                self.assertNotIn("private-test-secret", str(caught.exception))

    def test_invalid_profile_and_limits_fail_without_secret_diagnostics(self):
        for override in ({"CORE_AGENT_AUTH_MODE": "keycloak"},
                         {"CORE_AGENT_ENVIRONMENT": "test"},
                         {"WORKSPACE_STORAGE_TYPE": "unknown"},
                         {"WORKSPACE_MAX_BYTES": "NaN"}, {"WORKSPACE_MAX_FILES": "0"},
                         {"WORKSPACE_S3_MAX_INFLIGHT": "17"},
                         {"ARTIFACT_S3_SECRET_ACCESS_KEY": ""}):
            with self.subTest(override=override), patch.dict(os.environ, {**self.environment(), **override}, clear=True):
                with self.assertRaises(CoreError) as caught:
                    _workspace_storage()
                self.assertEqual(caught.exception.code, "CONFIG_INVALID")
                self.assertNotIn("private-test-secret", str(caught.exception))

    def test_platform_bucket_url_and_tenant_key_produce_one_signed_bucket_path(self):
        for suffix in ("/workspace", "/workspace/"):
            with self.subTest(suffix=suffix), patch.dict(os.environ, {
                    **self.environment(), "ARTIFACT_S3_ENDPOINT_URL": "https://s3.example.test" + suffix,
                    "ARTIFACT_S3_TENANT_ID": "storage-tenant"}, clear=True):
                settings = _workspace_storage()
            self.assertEqual(settings["endpoint_url"], "https://s3.example.test")
            self.assertEqual(settings["tenant_id"], "company")
            settings.pop("max_files")
            store = S3BlobStore(**settings)
            self.addCleanup(store.close)

            def inspect_request(request, **kwargs):
                self.assertEqual(request.url, "https://s3.example.test/workspace")
                self.assertIn(b"Credential=storage-tenant:test-key/", request.headers["Authorization"])
                raise RuntimeError("stop before network")

            store.client.meta.events.register("before-send.s3.HeadBucket", inspect_request)
            with self.assertRaisesRegex(RuntimeError, "stop before network"):
                store.check()

    def test_platform_key_qualification_and_workspace_overrides(self):
        for key, override, expected in (
            ("test-key", "", "storage-tenant:test-key"),
            ("storage-tenant:test-key", "", "storage-tenant:test-key"),
            ("other-tenant:test-key", "explicit-key", "explicit-key"),
        ):
            with self.subTest(key=key, override=override), patch.dict(os.environ, {
                    **self.environment(), "ARTIFACT_S3_ACCESS_KEY_ID": key,
                    "ARTIFACT_S3_TENANT_ID": "storage-tenant", "WORKSPACE_S3_ACCESS_KEY_ID": override,
                    "WORKSPACE_S3_ENDPOINT_URL": "https://explicit.example.test"}, clear=True):
                settings = _workspace_storage()
            self.assertEqual(settings["access_key_id"], expected)
            self.assertEqual(settings["endpoint_url"], "https://explicit.example.test")
        for key in ("other-tenant:test-key", "storage-tenant:", "storage-tenant:test:key"):
            with self.subTest(key=key), patch.dict(os.environ, {
                    **self.environment(), "ARTIFACT_S3_TENANT_ID": "storage-tenant",
                    "ARTIFACT_S3_ACCESS_KEY_ID": key}, clear=True), self.assertRaises(CoreError) as caught:
                _workspace_storage()
            self.assertEqual(caught.exception.code, "CONFIG_INVALID")
            self.assertNotIn(key, str(caught.exception))

    def test_platform_adaptation_does_not_strip_other_url_components(self):
        for override in (
            {"ARTIFACT_S3_ENDPOINT_URL": "https://s3.example.test/other-bucket"},
            {"ARTIFACT_S3_ENDPOINT_URL": "https://s3.example.test/workspace//"},
            {"ARTIFACT_S3_ENDPOINT_URL": "https://s3.example.test/workspace;params"},
            {"ARTIFACT_S3_ENDPOINT_URL": "https://s3.example.test/;params"},
            {"ARTIFACT_S3_ENDPOINT_URL": "https://s3.example.test/workspace;"},
            {"ARTIFACT_S3_ENDPOINT_URL": "https://s3.example.test/workspace/;"},
            {"ARTIFACT_S3_ENDPOINT_URL": "https://s3.example.test/;"},
            {"WORKSPACE_S3_ENDPOINT_URL": "https://s3.example.test/;"},
            {"ARTIFACT_S3_ENDPOINT_URL": "https://s3.example.test/workspace?query=private"},
            {"ARTIFACT_S3_ENDPOINT_URL": "https://user:password@s3.example.test/workspace"},
            {"ARTIFACT_S3_ENDPOINT_URL": "https://s3.example.test/workspace#fragment"},
            {"WORKSPACE_S3_ENDPOINT_URL": "https://s3.example.test/workspace"},
            {"ARTIFACT_S3_ENDPOINT_URL": "https://s3.example.test/workspace", "WORKSPACE_S3_BUCKET": "override"},
        ):
            with self.subTest(override=override), patch.dict(os.environ, {
                    **self.environment(), **override}, clear=True), self.assertRaises(CoreError) as caught:
                settings = _workspace_storage()
                settings.pop("max_files")
                S3BlobStore(**settings)
            self.assertEqual(caught.exception.code, "CONFIG_INVALID")
