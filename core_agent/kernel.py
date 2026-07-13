from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum


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

    def compile(
        self,
        *,
        enabled_capabilities,
        agent_profile,
        user_prompt,
        skill_instructions=(),
        retrieved=(),
        tool_data=(),
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
