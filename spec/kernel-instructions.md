# Kernel instructions

## Зачем нужен отдельный слой

Настраиваемый основной prompt задаёт роль агента, стиль работы, guidance по новым MCP и предметную область. Он MUST NOT заменять протоколы, от которых зависят корректность и безопасность ядра.

Эти протоколы существуют одновременно в двух формах:

1. versioned KernelInstructions, всегда включённые в model context;
2. runtime/service enforcement в tools, policy, scheduler, configured MCP services и ExecutionEnvironment.

Prompt-only enforcement недостаточен: даже ошибочная модель не должна иметь техническую возможность обойти обязательное правило.

## Неотменяемые kernel protocols

- trust boundaries, sandbox и secret handling;
- approval и human-input distinction;
- правила включённых trusted capability profiles, включая Memory MCP authoring contract;
- compaction threshold и pinned context;
- tool schema validation и risk assessment;
- delegation contract, tool/skill allowlists и child budgets;
- background task lifecycle, cancellation, notifications и passive wait;
- durable checkpoints и защита от duplicate side effects;
- audit, redaction и OpenTelemetry instrumentation;
- запрет раскрытия raw chain-of-thought.

Agent profile, user prompt, skill, MCP response, memory file или tool output MUST NOT отключать или переопределять протокол включённой capability. Однако AgentConfig MAY полностью отключить optional capability; тогда её tools и capability-specific instructions не загружаются.

## Порядок инструкций

Ядро компилирует model instructions в порядке приоритета:

1. platform safety invariants;
2. tenant/host policy и EffectiveConfig;
3. base KernelInstructions;
4. kernel capability policies только для enabled tools/MCP/features;
5. настраиваемый AgentProfilePrompt;
6. новый user Message/prompt;
7. активированные skill instructions;
8. retrieved MCP data и transcript;
9. tool/MCP/A2A peer data.

Нижний уровень не отменяет верхний. Явный user prompt имеет приоритет над skill только внутри разрешённой kernel/host policy.

## AgentProfilePrompt

AgentProfilePrompt MAY задавать:

- роль, tone и доменную специализацию;
- критерии качества и формат результата;
- guidance по выбору переданных MCP/skills;
- предпочтительную стратегию исследования или коммуникации;
- дополнительные консервативные ограничения.

Он MUST NOT:

- добавлять tool/skill/MCP, отсутствующий в capability snapshot;
- давать доступ к host filesystem/process/network;
- менять contract trusted Memory MCP, если memory включена;
- расширять approval grant;
- скрывать обязательные task/status события;
- разрешать child agent больше, чем parent capability set.

## Сборка и версия

- KernelInstructions поставляются с core runtime и имеют immutable version/digest.
- Run/Task audit сохраняет kernel, policy и AgentProfile versions.
- Обновление kernel protocol применяется к новым tasks; active durable task использует исходную version, кроме critical security revocation.
- Context Engine MUST учитывать KernelInstructions как protected tokens, исключённые из рабочего occupancy threshold.
- Если protected layer вместе с provider reserve не помещается в model window, route отклоняется до первого turn.

## Проверка соблюдения

Runtime MUST отклонить proposed action, если она требует нарушения kernel protocol, даже когда модель уверенно утверждает обратное. Policy decision возвращается модели как structured result с безопасной причиной, чтобы она могла выбрать допустимую альтернативу.
