from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from .errors import CoreError


# Only for exact validation of checkpoints written before buffered final answers.
LEGACY_RESPONSE_PHASE_INSTRUCTIONS = {
    "answer": "This is the public answer phase. Tools are unavailable. Write only the final answer "
        "for the Task recipient using verified context. Do not reveal hidden reasoning, "
        "internal tool calls or raw private owner questions/answers.",
    **{f"work_v{version}": start + "is available, call it with {} as your last tool call before writing the public "
        "final answer. Do not write a final-answer draft in that work turn."
        for version, start in (
            (1, "Complete work and select required response files first. When core_response_begin "),
            (2, "Complete work and attach any explicitly chosen response artifacts first. When core_response_begin "),
        )},
}


class InstructionSource(str, Enum):
    SAFETY = "safety"
    HOST_POLICY = "host_policy"
    BASE_KERNEL = "base_kernel"
    CAPABILITY_POLICY = "capability_policy"
    AGENT_PROFILE = "agent_profile"
    USER = "user"
    SKILL = "skill"
    RETRIEVED = "retrieved"
    TOOL_DATA = "tool_data"


@dataclass(frozen=True)
class InstructionSegment:
    source: InstructionSource
    text: str


@dataclass(frozen=True)
class CompiledInstructions:
    segments: tuple[InstructionSegment, ...]
    text: str
    protected_digest: str


class KernelCompiler:
    def __init__(self, safety, host_policy, base_kernel, capability_policies=None):
        self.safety = safety
        self.host_policy = host_policy
        self.base_kernel = base_kernel
        self.capability_policies = capability_policies or {}

    def snapshot(self, *, legacy=False):
        value = {"version": 1 if legacy else 2, "safety": self.safety, "host_policy": self.host_policy,
                 "base_kernel": self.base_kernel, "capability_policies": dict(self.capability_policies)}
        if legacy:
            value["response_phase_instructions"] = dict(getattr(
                self, "_legacy_response_phase_instructions", LEGACY_RESPONSE_PHASE_INSTRUCTIONS))
        value["digest"] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return value

    @classmethod
    def from_snapshot(cls, value):
        if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] not in {1, 2}
                or not all(isinstance(value.get(name), str) for name in ("safety", "host_policy", "base_kernel"))
                or not all(isinstance(value.get(name), dict)
                           and all(isinstance(key, str) and isinstance(text, str) for key, text in value[name].items())
                           for name in (("capability_policies", "response_phase_instructions")
                                        if value["version"] == 1 else ("capability_policies",)))
                or (value["version"] == 1 and set(value["response_phase_instructions"]) != {"answer", "work_v1", "work_v2"})):
            raise CoreError("CHECKPOINT_INVALID")
        compiler = cls(value["safety"], value["host_policy"], value["base_kernel"], dict(value["capability_policies"]))
        if value["version"] == 1:
            compiler._legacy_response_phase_instructions = dict(value["response_phase_instructions"])
        if compiler.snapshot(legacy=value["version"] == 1) != value:
            raise CoreError("CHECKPOINT_INVALID")
        return compiler

    def compile(
        self,
        *,
        enabled_capabilities,
        agent_profile,
        user_prompt,
        skill_instructions=(),
        retrieved=(),
        tool_data=(),
        legacy_response_phase=None,
        output_contract_version=2,
    ):
        segments = [
            InstructionSegment(InstructionSource.SAFETY, self.safety),
            InstructionSegment(InstructionSource.HOST_POLICY, self.host_policy),
            InstructionSegment(InstructionSource.BASE_KERNEL, self.base_kernel),
        ]
        segments += [
            InstructionSegment(
                InstructionSource.CAPABILITY_POLICY, self.capability_policies[name]
            )
            for name in sorted(enabled_capabilities)
            if name in self.capability_policies
        ]
        if legacy_response_phase:
            segments.append(InstructionSegment(InstructionSource.BASE_KERNEL,
                getattr(self, "_legacy_response_phase_instructions", LEGACY_RESPONSE_PHASE_INSTRUCTIONS)[
                    "answer" if legacy_response_phase == "answer" else
                                                 f"work_v{output_contract_version}"]))
        segments += [
            InstructionSegment(source, text)
            for source, text in (
                (InstructionSource.AGENT_PROFILE, agent_profile),
                (InstructionSource.USER, user_prompt),
            )
            if text
        ]
        segments += [
            InstructionSegment(InstructionSource.SKILL, value)
            for value in skill_instructions
        ]
        segments += [
            InstructionSegment(InstructionSource.RETRIEVED, value)
            for value in retrieved
        ]
        segments += [
            InstructionSegment(InstructionSource.TOOL_DATA, value)
            for value in tool_data
        ]
        protected = "\n".join(
            item.text
            for item in segments
            if item.source
            in {
                InstructionSource.SAFETY,
                InstructionSource.HOST_POLICY,
                InstructionSource.BASE_KERNEL,
                InstructionSource.CAPABILITY_POLICY,
            }
        )
        return CompiledInstructions(
            tuple(segments),
            "\n\n".join(item.text for item in segments),
            hashlib.sha256(protected.encode()).hexdigest(),
        )

    def public_model_result(self, result):
        return {
            key: value
            for key, value in result.items()
            if key not in {"reasoning", "reasoning_tokens"}
        }
