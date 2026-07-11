# Конфигурация агента

## Цель

AgentConfig создаёт конкретный экземпляр Core Agent из общего runtime. Он максимально гибко уменьшает доступные возможности: может отключить memory, terminal, filesystem mutations, delegation, background tasks, отдельные built-in/MCP tools или skills.

Конфигурация не является model prompt и не передаётся заново в каждой A2A Task.

## Три уровня

1. **PlatformConfig** — providers, credentials, stores, execution backends, tenant policy и infrastructure limits.
2. **AgentConfig** — identity/profile, model route, feature switches и capability filters конкретного агента.
3. **Task input** — только `prompt`, `mcp`, `skills` через A2A Message/Core extension.

Нижний уровень MAY дополнительно сузить capabilities, но не расширяет верхний.

## Пример

```yaml
schema_version: v1alpha1
agent:
  name: coding-agent
  profile_prompt: You are a repository coding agent.
model:
  route: coding-default
features:
  background_tasks: true
  delegation: true
  memory: optional
tools:
  builtins:
    default: deny
    allow:
      - core.terminal.exec
      - core.terminal.write
      - core.fs.apply_patch
      - core.task.*
      - core.delegate
    deny: []
  mcp:
    default: deny
    allow_servers: [repo, memory]
    allow_tools:
      repo: [search, read_file]
      memory: [search, read, create, update, split]
skills:
  default: deny
  allow: [database-review, release-notes]
context:
  compact_at_working_ratio: 0.90
  compact_to_working_ratio: 0.15
approval:
  mode: on_risk
execution:
  environment_profile: isolated-default
observability:
  otel_profile: production
```

## Feature switches

Минимальные optional features:

- `memory`: `disabled`, `optional`, `required`;
- `background_tasks`: boolean;
- `delegation`: boolean;
- `terminal`: boolean или результат tool filters;
- `filesystem_mutation`: boolean или результат tool filters;
- `mcp`: boolean;
- `skills`: boolean;
- `human_input`: boolean;
- `reusable_approval_grants`: boolean.

`disabled` memory означает:

- Memory MCP descriptor отклоняется или фильтруется по `required` semantics;
- memory tools отсутствуют в discovery/model context;
- memory-specific kernel policy не загружается;
- Core Agent не выполняет implicit memory retrieval/write.

`optional` разрешает Task передать Memory MCP. `required` требует подходящий descriptor до первого model turn.

## Tool filters

Built-in и MCP tools фильтруются после discovery, но до model context:

1. Platform/tenant deny policy;
2. AgentConfig feature switch;
3. AgentConfig server/tool allow/deny;
4. Task-provided MCP/skills;
5. delegation allowlist для child.

На каждом уровне deny имеет приоритет. Wildcard разрешён только в namespaced форме вроде `core.task.*` или `memory.*`; глобальный `*` SHOULD быть запрещён production policy.

Отключённый tool:

- не показывается модели;
- не может быть вызван по старому имени;
- не наследуется child Task;
- возвращает `CAPABILITY_DISABLED`, если вызов восстановлен из stale model output.

Protocol-internal A2A state transitions, policy checks, audit/redaction и ExecutionEnvironment isolation не являются model-callable tools и не отключаются tool filters.

Config validation MUST обнаруживать как минимум:

- `delegation: true` при отключённых background tasks или `core.delegate`;
- `memory: required`, если policy не разрешает ни одного Memory MCP server/tool;
- advertised A2A capability без runtime/transport implementation;
- tool allow pattern, полностью перекрытый deny policy;
- skill/MCP requirement, несовместимый с execution/network profile.

## MCP policy и roles

MCP descriptor MAY иметь host-validated role, например `memory`, `repository` или `issue_tracker`. Role не доверяется только потому, что пришла от клиента: AgentConfig/tenant policy сверяет server identity, transport target и optional integrity metadata.

Для каждого server можно настроить:

- allowed transports/targets;
- required/optional behavior;
- capability types tools/resources/prompts/sampling/elicitation;
- tool allow/deny patterns;
- secret refs;
- network/execution profile;
- trusted capability policy profile.

Memory mode относится только к MCP server с подтверждённой role `memory`.

## Skills policy

AgentConfig задаёт allowed sources, names, versions, permissions и default deny/allow. Task передаёт желаемые skills, но effective catalog содержит только пересечение Task request и AgentConfig/tenant policy.

## EffectiveConfig snapshot

Перед A2A Task runtime вычисляет immutable EffectiveConfig:

```text
EffectiveConfig = PlatformConfig ∩ tenant policy ∩ AgentConfig ∩ Task capabilities
```

Snapshot содержит versions/digests profile prompt, kernel policy packs, model route, tool schemas, MCP/skill locks, budgets, context thresholds, approval и execution/OTel profiles.

- Unknown config fields MUST отклоняться.
- Conflict MUST возвращать path и безопасную причину.
- Hot reload применяется только к новой Task, кроме security revocation.
- Agent Card генерируется из Effective AgentConfig и не рекламирует disabled capability.
- Audit/OTel записывают config version/digest без secret values.

## Границы гибкости

AgentConfig может отключить функциональность, но не может:

- исполнить команду на control-plane host;
- отключить schema validation, tenant isolation или secret redaction;
- раскрыть raw chain-of-thought;
- обойти policy/approval для оставшегося tool;
- объявить A2A capability, которой runtime фактически не поддерживает;
- превратить AgentProfilePrompt в capability grant.

## Ошибки конфигурации

- `CONFIG_INVALID` — schema/type/value invalid;
- `CONFIG_CONFLICT` — взаимоисключающие switches/policies;
- `CAPABILITY_DISABLED` — Task запросила отключённую capability;
- `REQUIRED_CAPABILITY_MISSING` — required memory/MCP/skill отсутствует;
- `TOOL_FILTER_EMPTY` — required workflow не имеет ни одного разрешённого tool.
