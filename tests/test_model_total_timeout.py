import contextlib
import json
import time
import unittest
from unittest.mock import patch

from core_agent.errors import CoreError
from core_agent.model import CompatibleHttpModel


def keepalive_forever():
    while True:
        time.sleep(0.01)
        yield b": keep-alive\n"


def finished_stream():
    frame = {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}
    yield ("data: " + json.dumps(frame) + "\n").encode()
    yield b"data: [DONE]\n"


class ModelTotalTimeoutTests(unittest.TestCase):
    def model(self, total_timeout):
        return CompatibleHttpModel(api_format="openai", model="m", base_url="http://model.test/v1",
                                   stream=True, total_timeout=total_timeout)

    def generate(self, model, stream):
        with patch.object(model, "_open", return_value=contextlib.nullcontext(stream)):
            return model.generate(context="ctx", tools={}, instructions="sys")

    def test_endless_keepalive_stream_is_cut_by_total_timeout(self):
        with self.assertRaises(CoreError) as raised:
            self.generate(self.model(0.05), keepalive_forever())
        self.assertEqual(raised.exception.code, "MODEL_UNAVAILABLE")
        self.assertTrue(raised.exception.retryable)

    def test_stream_within_total_timeout_completes(self):
        self.assertEqual(self.generate(self.model(5), finished_stream()).message, "done")

    def test_total_timeout_must_be_positive(self):
        with self.assertRaises(CoreError):
            self.model(0)


if __name__ == "__main__":
    unittest.main()
