from __future__ import annotations

import threading
from collections import OrderedDict

ADK_METADATA_PREFIX = "adk_"
ADK_THOUGHT_KEY = "adk_thought"
ADK_TYPE_KEY = "adk_type"
FUNCTION_CALL_TYPE = "function_call"
FUNCTION_RESPONSE_TYPE = "function_response"
PARTIAL_KEY = "partial"
DEFAULT_BUFFER_SIZE = 10
PUBLIC_REPLY_KEY = "core_agent_stream"


class ReplyHub:
    """Bounded transient public previews; workflow/Task remain authoritative."""

    def __init__(self, *, min_chars=DEFAULT_BUFFER_SIZE, max_tasks=64, max_bytes=256 * 1024):
        self.min_chars = max(0, int(min_chars))
        self.max_tasks = max(1, int(max_tasks))
        self.max_bytes = max(1, int(max_bytes))
        self._items = OrderedDict()
        self._lock = threading.Lock()
        self._sequence = 0

    def update(self, tenant, task_id, context_id, generation, text, *, force=False):
        text = (text or "").encode("utf-8")[:self.max_bytes].decode("utf-8", errors="ignore")
        key = (tenant, task_id)
        with self._lock:
            item = self._items.get(key)
            if item is not None and (generation < item["generation"] or (
                    generation == item["generation"] and item.get("superseded"))):
                return None
            if item is None or generation > item["generation"]:
                item = {"generation": generation, "sequence": 0, "text": "",
                        "context_id": context_id}
                self._items[key] = item
            self._items.move_to_end(key)
            while len(self._items) > self.max_tasks:
                self._items.popitem(last=False)
            if text == item["text"] or (not force and len(text) - len(item["text"]) < self.min_chars):
                return None
            item["text"] = text
            self._sequence += 1
            item["sequence"] = self._sequence
            return self._snapshot(item)

    @staticmethod
    def _snapshot(item):
        return dict(item)

    def latest(self, tenant, task_id):
        with self._lock:
            item = self._items.get((tenant, task_id))
            return self._snapshot(item) if item and item["sequence"] else None

    def supersede(self, tenant, task_id, *, generation=None, context_id=None):
        with self._lock:
            key = (tenant, task_id)
            item = self._items.get(key)
            if item is None:
                if generation is None:
                    return None
                item = {"generation": generation, "sequence": 0, "text": "", "context_id": context_id}
                self._items[key] = item
            if item.get("superseded") or (generation is not None and item["generation"] != generation):
                return None
            self._items.move_to_end(key)
            while len(self._items) > self.max_tasks:
                self._items.popitem(last=False)
            self._sequence += 1
            item.update(text="", superseded=True, sequence=self._sequence)
            return self._snapshot(item)

    def discard(self, tenant, task_id):
        with self._lock:
            self._items.pop((tenant, task_id), None)


def public_reply_metadata(snapshot):
    return {PARTIAL_KEY: True, PUBLIC_REPLY_KEY: {
        "version": 1, "generation": snapshot["generation"], "sequence": snapshot["sequence"],
        **({"superseded": True} if snapshot.get("superseded") else {}),
    }}


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
