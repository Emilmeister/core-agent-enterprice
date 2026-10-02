import os
import unittest
from unittest.mock import patch

from core_agent.app import _guardrail_classifier
from core_agent.errors import CoreError
from core_agent.model import CompatibleHttpModel


class GuardrailConfigurationTests(unittest.TestCase):
    def model(self):
        return CompatibleHttpModel(api_format="openai", model="agent", api_key="test-key",
                                   base_url="https://model.test/v1", stream=True, timeout=120,
                                   headers={"X-Test": "main"}, extra_body={"stream": True, "temperature": 0})

    def test_default_reuses_connection_without_mutating_agent_adapter(self):
        main = self.model()
        with patch.dict(os.environ, {}, clear=True):
            classifier = _guardrail_classifier(main)
        self.assertIsNot(classifier.model, main)
        self.assertEqual(classifier.model.endpoint, main.endpoint)
        self.assertEqual(classifier.model.api_key, main.api_key)
        self.assertEqual(classifier.model.timeout, 60)
        self.assertFalse(classifier.model.stream)
        self.assertNotIn("stream", classifier.model.extra_body)
        self.assertTrue(main.stream)
        self.assertTrue(main.extra_body["stream"])
        self.assertEqual((classifier.max_calls, classifier.max_input_tokens), (32, 100000))

    def test_explicit_provider_does_not_inherit_main_credentials_or_headers(self):
        for provider, api_format in (("anthropic", "anthropic"), ("minimax", "openai")):
            with self.subTest(provider=provider), patch.dict(os.environ, {
                "GUARDRAILS_LLM_PROVIDER": provider, "GUARDRAILS_LLM_MODEL": "detector",
                "GUARDRAILS_LLM_BASE_URL": "https://detector.test/v1", "GUARDRAILS_LLM_API_KEY": "separate-test-key",
                "GUARDRAILS_TIMEOUT_SECONDS": "12", "GUARDRAILS_MAX_CALLS": "3",
                "GUARDRAILS_MAX_INPUT_TOKENS": "5000",
            }, clear=True):
                classifier = _guardrail_classifier(self.model())
                self.assertEqual(classifier.model.api_format, api_format)
                self.assertEqual(classifier.model.model, "detector")
                self.assertEqual(classifier.model.api_key, "separate-test-key")
                self.assertEqual(classifier.model.headers, {})
                self.assertEqual((classifier.timeout_seconds, classifier.max_calls, classifier.max_input_tokens), (12, 3, 5000))

    def test_partial_override_or_invalid_limits_fail_before_any_model_request(self):
        invalid = [
            {"GUARDRAILS_LLM_MODEL": "detector"}, {"GUARDRAILS_LLM_API_KEY": "test-only"},
            {"GUARDRAILS_TIMEOUT_SECONDS": "nan"}, {"GUARDRAILS_TIMEOUT_SECONDS": "0"},
            {"GUARDRAILS_MAX_CALLS": "0"}, {"GUARDRAILS_MAX_INPUT_TOKENS": "invalid"},
        ]
        for environment in invalid:
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(CoreError) as error:
                    _guardrail_classifier(self.model())
                self.assertEqual(error.exception.code, "CONFIG_INVALID")


if __name__ == "__main__":
    unittest.main()
