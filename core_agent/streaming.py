from __future__ import annotations

ADK_METADATA_PREFIX = "adk_"
ADK_THOUGHT_KEY = "adk_thought"
ADK_TYPE_KEY = "adk_type"
FUNCTION_CALL_TYPE = "function_call"
FUNCTION_RESPONSE_TYPE = "function_response"
PARTIAL_KEY = "partial"
DEFAULT_BUFFER_SIZE = 10


def integrate_stream_chunk(buffer, chunk):
    """Merge a provider chunk that may be a delta or a full snapshot.

    Some providers resend the whole answer every frame; concatenating that would
    duplicate the text. Only an exact snapshot is recognised — a partial-overlap
    guess would silently delete characters from a legitimate delta.
    """
    if not chunk:
        return buffer
    if chunk.startswith(buffer):
        return chunk
    return buffer + chunk


class StreamBuffer:
    """Accumulate reasoning and response text and emit full cumulative snapshots.

    A snapshot is released once either channel has grown by ``min_chars`` so a
    token-level provider stream does not turn into one A2A frame per token.
    """

    def __init__(self, min_chars=DEFAULT_BUFFER_SIZE):
        self.min_chars = max(0, int(min_chars))
        self.response = ""
        self.reasoning = ""
        self._emitted_response = ""
        self._emitted_reasoning = ""

    @property
    def pending(self):
        return (
            self.response != self._emitted_response
            or self.reasoning != self._emitted_reasoning
        )

    def update(self, response, reasoning):
        """Record the latest snapshots and return one to emit, or None."""
        self.response = response or ""
        self.reasoning = reasoning or ""
        if not self.pending:
            return None
        grown = max(
            len(self.response) - len(self._emitted_response),
            len(self.reasoning) - len(self._emitted_reasoning),
        )
        if grown < self.min_chars:
            return None
        return self.flush()

    def flush(self):
        if not self.pending:
            return None
        self._emitted_response = self.response
        self._emitted_reasoning = self.reasoning
        return self.response, self.reasoning

    def reset(self):
        self.response = ""
        self.reasoning = ""
        self._emitted_response = ""
        self._emitted_reasoning = ""


class NullStreamPublisher:
    """No-op publisher used whenever a run has no attached A2A stream."""

    enabled = False
    streamed_text = ""

    def text(self, response, reasoning):
        return None

    def tool_call(self, call_id, name, arguments):
        return None

    def tool_result(self, call_id, name, response):
        return None

    def relay(self, parts):
        return None

    def flush(self):
        return None
