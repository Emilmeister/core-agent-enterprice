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
        for service_name in ("agent", "memory"):
            environment = self.services[service_name]["environment"]
            self.assertEqual(
                environment["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"], expected
            )
            self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT", environment)
            self.assertEqual(
                self.services[service_name]["depends_on"]["phoenix"]["condition"],
                "service_healthy",
            )

    def test_agent_profile_is_explicitly_configurable(self):
        environment = self.services["agent"]["environment"]
        self.assertIn("CORE_AGENT_NAME", environment)
        self.assertIn("CORE_AGENT_PROFILE", environment)
        self.assertEqual(
            environment["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"],
            "${OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT:-true}",
        )
        self.assertEqual(
            environment["CORE_AGENT_LOG_LEVEL"], "${CORE_AGENT_LOG_LEVEL:-INFO}"
        )
        self.assertEqual(
            environment["CORE_AGENT_LOG_CONTENT"],
            "${CORE_AGENT_LOG_CONTENT:-true}",
        )
        self.assertEqual(
            environment["CORE_AGENT_LOG_MAX_CHARS"],
            "${CORE_AGENT_LOG_MAX_CHARS:-12000}",
        )


if __name__ == "__main__":
    unittest.main()
