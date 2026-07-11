from __future__ import annotations

from dataclasses import dataclass

from .errors import CoreError


@dataclass(frozen=True)
class ToolRequest:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ModelResponse:
    message: str | None = None
    tool_requests: tuple[ToolRequest, ...] = ()
    continue_reasoning: bool = False
    reasoning: str | None = None


@dataclass(frozen=True)
class ModelCall:
    context: str
    tools: frozenset[str]
    instructions: str


class ScriptedModel:
    def __init__(self, responses):
        self._responses = list(responses)
        self._calls = []

    @property
    def calls(self):
        return tuple(self._calls)

    def generate(self, *, context, tools, instructions):
        self._calls.append(ModelCall(context, frozenset(tools), instructions))
        if not self._responses:
            raise CoreError("MODEL_UNAVAILABLE")
        return self._responses.pop(0)


@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int
    tool_use: bool
    structured_output: bool
    modalities: set[str]


@dataclass(frozen=True)
class ModelRoute:
    name: str
    capabilities: ModelCapabilities
    region: str
    data_classes: set[str]
    cost: int


@dataclass(frozen=True)
class FallbackDecision:
    route: ModelRoute
    compaction_required: bool
    do_not_replay_tool_call_ids: frozenset[str]


class ModelRouter:
    def __init__(self, routes):
        self.routes = list(routes)

    def select(
        self, *, required_context, modalities, data_class, allowed_regions, max_cost
    ):
        candidates = [
            route
            for route in self.routes
            if route.capabilities.context_window >= required_context
            and set(modalities) <= set(route.capabilities.modalities)
            and data_class in route.data_classes
            and route.region in allowed_regions
            and route.cost <= max_cost
        ]
        if not candidates:
            raise CoreError("MODEL_UNAVAILABLE")
        return min(candidates, key=lambda route: (route.cost, route.name))

    def fallback(self, *, failed_route, active_context_tokens, completed_tool_call_ids):
        candidates = [route for route in self.routes if route.name != failed_route]
        if not candidates:
            raise CoreError("MODEL_UNAVAILABLE")
        route = min(candidates, key=lambda item: (item.cost, item.name))
        return FallbackDecision(
            route,
            active_context_tokens > route.capabilities.context_window,
            frozenset(completed_tool_call_ids),
        )
