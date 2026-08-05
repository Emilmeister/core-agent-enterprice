import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ComposeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.services = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )["services"]

    def test_phoenix_is_version_pinned_and_uses_postgres(self):
        phoenix = self.services["phoenix"]
        self.assertEqual(
            phoenix["image"],
            "arizephoenix/phoenix:version-17.5.0@sha256:"
            "936d39fc27ea5b7807d39bdc184b1033222a413dc185858817cbf51d2bbf96e4",
        )
        self.assertTrue(
            phoenix["environment"]["PHOENIX_SQL_DATABASE_URL"].startswith(
                "postgresql://"
            )
        )
        self.assertEqual(
            phoenix["environment"]["PHOENIX_SQL_DATABASE_SCHEMA"], "phoenix"
        )
        self.assertIn("@sha256:", phoenix["image"])

    def test_core_services_send_only_traces_to_phoenix(self):
        expected = (
            "${OTEL_EXPORTER_OTLP_TRACES_ENDPOINT:-http://phoenix:6006/v1/traces}"
        )
        for service_name in ("agent",):
            environment = self.services[service_name]["environment"]
            self.assertEqual(
                environment["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"], expected
            )
            self.assertNotIn("OTEL_ENDPOINT", environment)
            self.assertEqual(
                self.services[service_name]["depends_on"]["phoenix"]["condition"],
                "service_healthy",
            )

    def test_agent_profile_is_explicitly_configurable(self):
        environment = self.services["agent"]["environment"]
        self.assertIn("AGENT_NAME", environment)
        self.assertEqual(environment["AGENT_SYSTEM_PROMPT"], "${AGENT_SYSTEM_PROMPT:-}")
        self.assertEqual(environment["THINKING_LEVEL"], "${THINKING_LEVEL:-}")
        self.assertEqual(
            environment["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"],
            "${OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT:-true}",
        )
        self.assertEqual(environment["LOG_LEVEL"], "${LOG_LEVEL:-INFO}")
        self.assertEqual(
            environment["CORE_AGENT_LOG_CONTENT"],
            "${CORE_AGENT_LOG_CONTENT:-true}",
        )
        self.assertEqual(
            environment["CORE_AGENT_LOG_MAX_CHARS"],
            "${CORE_AGENT_LOG_MAX_CHARS:-12000}",
        )
        self.assertEqual(
            environment["CORE_AGENT_RUNTIME_MODE"],
            "${CORE_AGENT_RUNTIME_MODE:-with_terminal}",
        )
        self.assertIn("CORE_AGENT_ALLOWED_BUILTIN_TOOLS", environment)
        self.assertIn(
            "core_python_exec", environment["CORE_AGENT_ALLOWED_BUILTIN_TOOLS"]
        )

    def test_transfer_scheme_variables_are_wired_through_compose(self):
        environment = self.services["agent"]["environment"]
        for name, expected in (
            ("AGENT_NAME", "${AGENT_NAME:-core-agent}"),
            ("THINKING_ENABLED", "${THINKING_ENABLED:-true}"),
            ("A2A_STREAMING_BUFFER_SIZE", "${A2A_STREAMING_BUFFER_SIZE:-10}"),
            ("ARTIFACT_STORAGE_TYPE", "${ARTIFACT_STORAGE_TYPE:-in-memory}"),
            ("REMOTE_AGENTS", "${REMOTE_AGENTS:-}"),
            ("SEND_MESSAGE_API_KEY", "${SEND_MESSAGE_API_KEY:-}"),
            ("USER_ID", "${USER_ID:-anonymous}"),
            ("TASK_STORAGE_TYPE", "${TASK_STORAGE_TYPE:-postgres}"),
            ("CORE_AGENT_MEMORY", "${CORE_AGENT_MEMORY:-optional}"),
            ("MEMORY_STORAGE_TYPE", "${MEMORY_STORAGE_TYPE:-in-memory}"),
            ("MEMORY_SEARCH_LIMIT", "${MEMORY_SEARCH_LIMIT:-10}"),
            ("EMBEDDING_MODEL", "${EMBEDDING_MODEL:-}"),
            ("EMBEDDING_API_BASE", "${EMBEDDING_API_BASE:-}"),
            ("EMBEDDING_API_KEY", "${EMBEDDING_API_KEY:-}"),
            ("EMBEDDING_DIMENSION", "${EMBEDDING_DIMENSION:-768}"),
            ("MCP_ALLOWED_SERVERS", "${MCP_ALLOWED_SERVERS:-}"),
            ("MCP_ALLOWED_TOOLS", "${MCP_ALLOWED_TOOLS:-}"),
            ("MCP_READ_ONLY_TOOLS", "${MCP_READ_ONLY_TOOLS:-}"),
            ("MCP_HEADERS_JSON", "${MCP_HEADERS_JSON:-}"),
            ("MCP_TIMEOUT", "${MCP_TIMEOUT:-30.0}"),
            ("MCP_SSE_READ_TIMEOUT", "${MCP_SSE_READ_TIMEOUT:-300.0}"),
            (
                "MCP_COLD_START_TIMEOUT_SECONDS",
                "${MCP_COLD_START_TIMEOUT_SECONDS:-300.0}",
            ),
        ):
            with self.subTest(name=name):
                self.assertEqual(environment[name], expected)
        self.assertEqual(environment["SESSION_STORAGE_TYPE"], "postgres")


if __name__ == "__main__":
    unittest.main()
