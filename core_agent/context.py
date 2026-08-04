from __future__ import annotations

from dataclasses import dataclass
import json

from .errors import CoreError


@dataclass(frozen=True)
class ContextBudget:
    total_tokens: int
    system_and_kernel_tokens: int
    tool_schema_tokens: int
    output_reserve: int
    compact_at: float = 0.90
    compact_to: float = 0.15

    def __post_init__(self):
        if (
            self.working_capacity <= 0
            or self.compact_at != 0.90
            or not 0.10 <= self.compact_to <= 0.15
        ):
            raise CoreError("CONTEXT_UNRECOVERABLE")

    @property
    def base_tokens(self):
        return (
            self.system_and_kernel_tokens
            + self.tool_schema_tokens
            + self.output_reserve
        )

    @property
    def working_capacity(self):
        return self.total_tokens - self.base_tokens

    def working_ratio(self, tokens):
        return tokens / self.working_capacity

    def should_compact(self, tokens):
        return self.working_ratio(tokens) >= self.compact_at


@dataclass(frozen=True)
class ContextItem:
    kind: str
    content: str
    tokens: int
    pinned: bool = False
    provider_replay: dict | None = None


@dataclass(frozen=True)
class CompactionEvent:
    before_working_tokens: int
    after_working_tokens: int
    replaced_sequence_range: tuple[int, int]


@dataclass(frozen=True)
class ContextState:
    active: tuple[ContextItem, ...]
    transcript: tuple[ContextItem, ...]
    sequence_range: tuple[int, int]
    event: CompactionEvent | None = None

    @property
    def working_tokens(self):
        return sum(item.tokens for item in self.active)


class Compactor:
    """Replace unpinned history with a structured summary once the budget fills.

    `enabled` turns compaction off entirely, `interval` forces a compaction every
    N model turns even below the ratio threshold, and `overlap` keeps that many of
    the most recent unpinned items verbatim next to the summary so the model does
    not lose the immediate conversational thread.
    """

    def __init__(self, budget, summarizer, *, enabled=True, interval=0, overlap=0):
        self.budget = budget
        self.summarizer = summarizer
        self.enabled = bool(enabled)
        self.interval = max(0, int(interval))
        self.overlap = max(0, int(overlap))

    def due(self, turns):
        return bool(self.enabled and self.interval and turns and turns % self.interval == 0)

    def maybe_compact(self, state, *, turns=0):
        if not self.enabled:
            return state
        if self.budget.should_compact(state.working_tokens) or self.due(turns):
            return self.compact(state, forced=self.due(turns))
        return state

    def compact(self, state, *, forced=False):
        before = state.working_tokens
        if not self.enabled or (not forced and not self.budget.should_compact(before)):
            return state
        pinned = tuple(item for item in state.active if item.pinned)
        unpinned = tuple(item for item in state.active if not item.pinned)
        overlap = unpinned[len(unpinned) - self.overlap :] if self.overlap else ()
        candidates = unpinned[: len(unpinned) - len(overlap)]
        if not candidates:
            return state
        pinned_tokens = sum(item.tokens for item in pinned + overlap)
        target = int(self.budget.working_capacity * self.budget.compact_to)
        if pinned_tokens > target:
            raise CoreError("CONTEXT_UNRECOVERABLE")
        summary = self.summarizer(candidates, target - pinned_tokens)
        sections = (
            "Goal:",
            "Constraints:",
            "Decisions:",
            "Completed:",
            "Artifacts:",
            "Pending:",
            "Failures:",
        )
        if not isinstance(summary, ContextItem) or summary.kind != "summary":
            raise CoreError("CONTEXT_UNRECOVERABLE")
        if not all(section in summary.content for section in sections):
            raise CoreError("CONTEXT_UNRECOVERABLE")
        active = pinned + (summary,) + overlap
        after = sum(item.tokens for item in active)
        if after > target or (
            not forced and after < int(self.budget.working_capacity * 0.10)
        ):
            raise CoreError("CONTEXT_UNRECOVERABLE")
        return ContextState(
            active,
            state.transcript,
            state.sequence_range,
            CompactionEvent(before, after, state.sequence_range),
        )


class StructuredSummarizer:
    """Conservative local summary: provenance is retained without another model call."""

    def __init__(self, token_counter):
        self.token_counter = token_counter

    def __call__(self, items, max_tokens):
        if max_tokens <= 0:
            raise CoreError("CONTEXT_UNRECOVERABLE")
        records = [
            {"kind": item.kind, "content": item.content}
            for item in items
        ]
        prefix = (
            "STRUCTURED SUMMARY\n"
            "Goal: preserved verbatim in pinned prompt\n"
            "Constraints: preserved in pinned runtime contracts\n"
            "Decisions: see provenance records below\n"
            "Completed: "
        )
        suffix = (
            "\nArtifacts: exact references remain in provenance records\n"
            "Pending: unresolved pinned state remains outside this summary\n"
            "Failures: see records with failure status"
        )
        encoded = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
        content = prefix + encoded + suffix
        while self.token_counter(content) > max_tokens and encoded:
            encoded = encoded[: max(0, int(len(encoded) * 0.9))]
            content = prefix + encoded + suffix
        tokens = self.token_counter(content)
        if tokens > max_tokens:
            raise CoreError("CONTEXT_UNRECOVERABLE")
        return ContextItem("summary", content, tokens)
