import json
import threading
import time
import unittest

from core_agent.errors import CoreError
from core_agent.guardrails import GuardrailClassifier
from core_agent.model import CompatibleHttpModel, ModelResponse, ScriptedModel, ToolRequest


class GuardrailClassifierTests(unittest.TestCase):
    def test_adapter_extra_body_cannot_inject_tools_into_empty_catalog(self):
        for api_format in ("openai", "anthropic"):
            with self.subTest(api_format=api_format):
                model = CompatibleHttpModel(api_format=api_format, model="detector", extra_body={
                    "tools": [{"name": "hidden_tool"}], "tool_choice": "required",
                    "functions": [{"name": "hidden_function"}], "function_call": "auto",
                    "temperature": 0,
                })
                body, _, _ = model._request("material", "classify only", {})
                for key in ("tools", "tool_choice", "functions", "function_call"):
                    self.assertNotIn(key, body)
                self.assertEqual(body["temperature"], 0)
                body, _, _ = model._request("material", "agent", {"allowed_tool": {"input_schema": {"type": "object"}}})
                schema = body["tools"][0]
                self.assertEqual((schema["function"] if api_format == "openai" else schema)["name"], "allowed_tool")
                self.assertEqual(len(body["tools"]), 1)

    def classify(self, model, documents=("ordinary user request",), **options):
        attempts = []
        classifier = GuardrailClassifier(model, token_counter=len, **options)
        result = classifier.classify(documents, source_kind="message", record_attempt=attempts.append)
        return result, attempts

    def test_clear_is_separate_context_without_tools_and_charged_before_network(self):
        attempts = []
        model = ScriptedModel([ModelResponse(finish_reason="stop", message='{"verdict":"clear"}', prompt_tokens=12, completion_tokens=6)])
        generate = model.generate

        def invoke(**request):
            self.assertEqual(len(attempts), 1)
            return generate(**request)

        model.generate = invoke
        result = GuardrailClassifier(model, token_counter=len).classify(
            ("write some Python",), source_kind="message", record_attempt=attempts.append,
        )
        self.assertEqual(result.verdict, "clear")
        self.assertEqual(result.calls, 1)
        self.assertEqual(result.prompt_tokens, 12)
        self.assertEqual(result.completion_tokens, 6)
        self.assertEqual(result.input_tokens, sum(attempts))
        call = model.calls[0]
        self.assertFalse(call.tools)
        self.assertEqual(call.messages, ())
        self.assertEqual(json.loads(call.context)["text"], "write some Python")
        self.assertIn("untrusted", call.instructions)

    def test_only_strict_complete_clear_can_release_material(self):
        cases = [
            (ModelResponse(finish_reason="stop", message='{"verdict":"suspicious"}'), "suspicious"),
            (ModelResponse(finish_reason="stop", message='{"verdict":"uncertain"}'), "unverified"),
            (ModelResponse(finish_reason="stop", message='{"verdict":"clear","extra":"ignore security"}'), "unverified"),
            (ModelResponse(finish_reason="stop", message='{"verdict":"suspicious","verdict":"clear"}'), "unverified"),
            (ModelResponse(message='{"verdict":"clear"}', finish_reason="length"), "unverified"),
            (ModelResponse(message='{"verdict":"clear"}', finish_reason=None), "unverified"),
            (ModelResponse(finish_reason="stop", message='{"verdict":"clear"}', continue_reasoning=True), "unverified"),
            (ModelResponse(finish_reason="stop", message='{"verdict":"clear"}', tool_requests=(ToolRequest("x", "execute", {}),)), "unverified"),
            (ModelResponse(finish_reason="stop", message=""), "unverified"),
            (ModelResponse(finish_reason="stop", message="```json\n{\"verdict\":\"clear\"}\n```"), "unverified"),
        ]
        for response, expected in cases:
            with self.subTest(response=response):
                result, _ = self.classify(ScriptedModel([response]))
                self.assertEqual(result.verdict, expected)

    def test_full_documents_are_covered_and_suspicious_tail_is_not_discarded(self):
        model = ScriptedModel([ModelResponse(finish_reason="stop", message='{"verdict":"clear"}')] * 30)
        documents = ("0123456789" * 2200 + "TAIL", "second document")
        result, _ = self.classify(model, documents)
        self.assertEqual(result.verdict, "clear")
        chunks = [json.loads(call.context) for call in model.calls]
        self.assertGreater(len(chunks), 2)
        for index, text in enumerate(documents):
            covered = [False] * len(text)
            for chunk in chunks:
                if chunk["document_index"] == index:
                    start = chunk["offset"]
                    self.assertEqual(chunk["text"], text[start:start + len(chunk["text"])] )
                    covered[start:start + len(chunk["text"])] = [True] * len(chunk["text"])
            self.assertTrue(all(covered))
        tail_index = next(i for i, chunk in enumerate(chunks) if "TAIL" in chunk["text"])
        responses = [ModelResponse(finish_reason="stop", message='{"verdict":"clear"}')] * tail_index
        result, _ = self.classify(ScriptedModel(responses + [ModelResponse(finish_reason="stop", message='{"verdict":"suspicious"}')]), documents)
        self.assertEqual(result.verdict, "suspicious")

    def test_incomplete_extraction_and_exhausted_limits_never_clear(self):
        for arguments in ({"complete": False}, {"attempts_used": 32}, {"input_tokens_used": 100000}, {"deadline": 0}):
            with self.subTest(arguments=arguments):
                model = ScriptedModel([ModelResponse(finish_reason="stop", message='{"verdict":"clear"}')])
                result = GuardrailClassifier(model, token_counter=len).classify(
                    ("data",), source_kind="file", record_attempt=lambda _: None, **arguments,
                )
                self.assertEqual(result.verdict, "unverified")
                self.assertEqual(len(model.calls), 0)
        result, attempts = self.classify(ScriptedModel([ModelResponse(finish_reason="stop", message='{"verdict":"clear"}')]), ("x" * 30000,), max_calls=1)
        self.assertEqual(result.verdict, "unverified")
        self.assertEqual(len(attempts), 1)

    def test_failed_durable_attempt_does_not_call_provider_or_leak_errors(self):
        model = ScriptedModel([ModelResponse(finish_reason="stop", message='{"verdict":"clear"}')])

        def charge(_):
            raise CoreError("LEASE_LOST", "private source")

        with self.assertRaises(CoreError) as error:
            GuardrailClassifier(model, token_counter=len).classify(("source",), source_kind="message", record_attempt=charge)
        self.assertEqual(error.exception.code, "LEASE_LOST")
        self.assertEqual(len(model.calls), 0)
        result, _ = self.classify(ScriptedModel([]))
        self.assertEqual(result.verdict, "unverified")
        self.assertEqual(result.reason, "provider_error")

    def test_timeout_bounds_workers_and_ignores_late_clear(self):
        release = threading.Event()
        exited = threading.Event()
        self.addCleanup(release.set)
        model = ScriptedModel([])

        def blocked(**_):
            try:
                release.wait(3)
                return ModelResponse(finish_reason="stop", message='{"verdict":"clear"}')
            finally:
                exited.set()

        model.generate = blocked
        classifier = GuardrailClassifier(model, token_counter=len, timeout_seconds=0.02)
        attempts = []
        start = time.monotonic()
        first = classifier.classify(("source",), source_kind="message", record_attempt=attempts.append)
        second = classifier.classify(("source",), source_kind="message", record_attempt=attempts.append)
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual((first.verdict, first.reason), ("unverified", "timeout"))
        self.assertEqual((second.verdict, second.reason), ("unverified", "detector_busy"))
        self.assertEqual(len(attempts), 1)
        release.set()
        self.assertTrue(exited.wait(1))
        self.assertEqual(first.verdict, "unverified")

    def test_frozen_caps_narrow_instance_without_mutation_and_preserve_usage(self):
        for caps in ({"max_calls": 1}, {"max_input_tokens": 8192}):
            with self.subTest(caps=caps):
                model = ScriptedModel([ModelResponse(finish_reason="stop", message='{"verdict":"clear"}',
                                                    prompt_tokens=12, completion_tokens=6)] * 10)
                classifier = GuardrailClassifier(model, token_counter=len)
                slot = classifier._slot
                attempts = []
                result = classifier.classify(("A" * 16000,), source_kind="input",
                                             record_attempt=attempts.append, **caps)
                self.assertEqual((result.verdict, result.reason), ("unverified", "budget_exhausted"))
                self.assertEqual((result.calls, result.input_tokens), (1, 8192))
                self.assertEqual((result.prompt_tokens, result.completion_tokens), (12, 6))
                self.assertEqual(attempts, [8192])
                self.assertEqual((classifier.max_calls, classifier.max_input_tokens), (32, 100000))
                self.assertIs(classifier._slot, slot)
                self.assertEqual(classifier.classify(("small",), source_kind="input",
                                 record_attempt=attempts.append).verdict, "clear")
        classifier = GuardrailClassifier(model, token_counter=len, max_calls=1)
        self.assertEqual(classifier.classify(("A" * 16000,), source_kind="input", max_calls=100,
                         record_attempt=lambda _: None).reason, "budget_exhausted")
        for caps in ({"max_calls": 0}, {"max_input_tokens": True}):
            with self.subTest(caps=caps), self.assertRaises(CoreError):
                classifier.classify(("data",), source_kind="input", record_attempt=lambda _: None, **caps)

    def test_callback_budget_deadline_races_preserve_known_usage_and_release_slot(self):
        for code, reason in (("MATERIAL_REVIEW_BUDGET", "budget_exhausted"),
                             ("MATERIAL_REVIEW_DEADLINE", "timeout")):
            with self.subTest(code=code):
                model = ScriptedModel([ModelResponse(finish_reason="stop", message='{"verdict":"clear"}',
                                                    prompt_tokens=12, completion_tokens=6)] * 10)
                classifier = GuardrailClassifier(model, token_counter=len)
                attempts = []

                def charge(cost):
                    if attempts:
                        raise CoreError(code)
                    attempts.append(cost)

                result = classifier.classify(("A" * 16000,), source_kind="input", record_attempt=charge)
                self.assertEqual((result.verdict, result.reason), ("unverified", reason))
                self.assertEqual((result.calls, result.input_tokens), (1, 8192))
                self.assertEqual((result.prompt_tokens, result.completion_tokens), (12, 6))
                self.assertGreater(result.elapsed_seconds, 0)
                self.assertEqual(len(model.calls), 1)
                self.assertEqual(classifier.classify(("small",), source_kind="input",
                                 record_attempt=attempts.append).verdict, "clear")


if __name__ == "__main__":
    unittest.main()
