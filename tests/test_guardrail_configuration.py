import os
import unittest
from copy import deepcopy
from unittest.mock import patch

from core_agent.app import _guardrail_classifier
from core_agent.errors import CoreError
from core_agent.model import CompatibleHttpModel


class GuardrailConfigurationTests(unittest.TestCase):
    def model(self, api_format="openai"):
        return CompatibleHttpModel(api_format=api_format, model="agent", api_key="test-key",
                                   base_url="https://model.test/v1", stream=True, timeout=120,
                                   headers={"X-Test": "main"}, extra_body={"stream": True, "temperature": 0})

    def assert_verdict_schema(self, body, api_format):
        schema = {"type": "object", "properties": {"verdict": {
            "type": "string", "enum": ["clear", "suspicious", "uncertain"]}},
            "required": ["verdict"], "additionalProperties": False}
        if api_format == "openai":
            response_format = body.get("response_format", {})
            self.assertEqual(response_format.get("type"), "json_schema")
            definition = response_format["json_schema"]
            self.assertIs(definition.get("strict"), True)
            self.assertTrue(definition.get("name"))
            self.assertEqual(definition.get("schema"), schema)
        else:
            self.assertEqual(body.get("output_config", {}).get("format"),
                             {"type": "json_schema", "schema": schema})
        for field in ("tools", "tool_choice", "functions", "function_call"):
            self.assertNotIn(field, body)

    def test_default_reuses_connection_without_mutating_agent_adapter(self):
        for api_format in ("openai", "anthropic"):
            with self.subTest(api_format=api_format), patch.dict(os.environ, {}, clear=True):
                main = self.model(api_format)
                other_schema = {"type": "object", "properties": {"answer": {"type": "string"}},
                                "required": ["answer"], "additionalProperties": False}
                if api_format == "openai":
                    main.extra_body["response_format"] = {"type": "json_schema", "json_schema": {
                        "name": "main_response", "strict": True, "schema": other_schema}}
                else:
                    main.extra_body["output_config"] = {"effort": "low", "format": {
                        "type": "json_schema", "schema": other_schema}}
                original = deepcopy(main.extra_body)
                classifier = _guardrail_classifier(main)
                self.assertIsNot(classifier.model, main)
                self.assertEqual(classifier.model.endpoint, main.endpoint)
                self.assertEqual(classifier.model.api_key, main.api_key)
                self.assertEqual(classifier.model.timeout, 60)
                self.assertFalse(classifier.model.stream)
                self.assertNotIn("stream", classifier.model.extra_body)
                self.assertTrue(main.stream)
                self.assertEqual((classifier.max_calls, classifier.max_input_tokens), (32, 100000))
                body, _, _ = classifier.model._request("material", "classify", {})
                self.assert_verdict_schema(body, api_format)
                if api_format == "anthropic":
                    self.assertEqual(body["output_config"]["effort"], "low")
                self.assertEqual(main.extra_body, original)

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
                body, _, _ = classifier.model._request("material", "classify", {})
                self.assert_verdict_schema(body, api_format)

    def test_schema_rejection_is_unverified_without_retry_or_format_fallback(self):
        for api_format in ("openai", "anthropic"):
            with self.subTest(api_format=api_format), patch.dict(os.environ, {}, clear=True):
                classifier = _guardrail_classifier(self.model(api_format))
                attempts = []
                with self.assertLogs("core_agent.runtime", "WARNING") as captured, patch.object(classifier.model, "_post", side_effect=CoreError(
                        "MODEL_UNAVAILABLE", "private schema rejection")) as post:
                    result = classifier.classify(("ordinary material",), source_kind="tool_arguments",
                                                 record_attempt=attempts.append)
                self.assertEqual((result.verdict, result.reason), ("unverified", "provider_error"))
                self.assertEqual(result.calls, 1)
                self.assertEqual(len(attempts), 1)
                self.assertEqual(result.input_tokens, sum(attempts))
                post.assert_called_once()
                self.assert_verdict_schema(post.call_args.args[0], api_format)
                self.assertNotIn("private", str(result))
                logged = "\n".join(captured.output)
                self.assertIn("MODEL_UNAVAILABLE", logged)
                self.assertIn("error_type", logged)
                self.assertNotIn("private schema rejection", logged)
                self.assertNotIn("ordinary material", logged)

    def test_invalid_inherited_anthropic_output_config_fails_safely(self):
        for value in (None, "private invalid options", ["private invalid options"]):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True), \
                    patch.object(CompatibleHttpModel, "_post") as post:
                main = self.model("anthropic")
                main.extra_body["output_config"] = value
                with self.assertRaises(CoreError) as error:
                    _guardrail_classifier(main)
                self.assertEqual(error.exception.code, "CONFIG_INVALID")
                self.assertNotIn("private", str(error.exception))
                self.assertEqual(main.extra_body["output_config"], value)
                post.assert_not_called()

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
