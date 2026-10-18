"""Request-local reasoning policy for structured LLM response formats."""

from copy import deepcopy

from .errors import CoreError


STRUCTURED_OUTPUT_DEFAULT = {"reasoning": "auto", "compatibility": "auto"}


def validate_structured_output(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"reasoning", "compatibility"}
        or not isinstance(value["reasoning"], str)
        or value["reasoning"] not in {"auto", "off", "inherit"}
        or not isinstance(value["compatibility"], str)
        or value["compatibility"] not in {
            "auto", "chat_template", "thinking", "reasoning_effort",
        }
    ):
        raise CoreError("SETTINGS_INVALID", "Invalid structured_output policy")
    return {"reasoning": value["reasoning"], "compatibility": value["compatibility"]}


def normalize_structured_output_body(
    body, policy, *, provider=None, native_anthropic=False, declared_parameters=None,
):
    """Apply the policy after merging provider parameters, without mutating them.

    ``provider`` must be explicitly configured, never inferred from a hostname
    or model alias. Adapters that synthesize provider fields pass their original
    ``declared_parameters`` for compatibility detection. A None policy preserves
    the behavior of version-less accepted runs.
    """
    if policy is None:
        return dict(body)
    policy = validate_structured_output(policy)
    response_format = body.get("response_format")
    output_config = body.get("output_config")
    output_format = output_config.get("format") if isinstance(output_config, dict) else None
    structured = (
        isinstance(response_format, dict)
        and response_format.get("type") in ("json_schema", "json_object")
    ) or (isinstance(output_format, dict) and output_format.get("type") == "json_schema")
    if not structured or policy["reasoning"] == "inherit":
        return dict(body)

    style = "thinking" if native_anthropic else policy["compatibility"]
    declared = body if declared_parameters is None else declared_parameters
    template = declared.get("chat_template_kwargs")
    thinking = declared.get("thinking")
    if style == "auto":
        configured_provider = provider.lower() if isinstance(provider, str) else None
        if configured_provider == "vllm" or (
            isinstance(template, dict) and "enable_thinking" in template
        ):
            style = "chat_template"
        elif configured_provider == "deepseek" or (
            isinstance(thinking, dict) and "type" in thinking
        ):
            style = "thinking"
        elif policy["reasoning"] == "off":
            raise CoreError("CONFIG_INVALID", "STRUCTURED_REASONING_UNSUPPORTED")
        else:
            return dict(body)

    result = deepcopy(body)
    for key in ("reasoning_effort", "thinking", "enable_thinking"):
        result.pop(key, None)
    for name, keys in (
        ("chat_template_kwargs", ("enable_thinking", "thinking", "reasoning_effort")),
        ("output_config", ("effort",)),
        ("reasoning", ("effort",)),
    ):
        nested = result.get(name)
        if isinstance(nested, dict):
            for key in keys:
                nested.pop(key, None)
            if not nested:
                result.pop(name)
    if style == "chat_template":
        template = result.get("chat_template_kwargs", {})
        if not isinstance(template, dict):
            raise CoreError("CONFIG_INVALID", "chat_template_kwargs must be an object")
        result["chat_template_kwargs"] = {**template, "enable_thinking": False}
    elif style == "thinking":
        result["thinking"] = {"type": "disabled"}
    else:
        result["reasoning_effort"] = "none"
    return result
