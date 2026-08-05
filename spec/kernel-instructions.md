# Kernel instructions

## Зачем нужен отдельный слой

Настраиваемый основной prompt задаёт роль агента, стиль работы, guidance по новым MCP и предметную область. Он MUST NOT заменять протоколы, от которых зависят корректность и безопасность ядра.

Эти протоколы существуют одновременно в двух формах:

1. versioned KernelInstructions, всегда включённые в model context;
2. runtime/service enforcement в tools, policy, scheduler, configured MCP services и TerminalSession manager.

Prompt-only enforcement недостаточен: даже ошибочная модель не должна иметь техническую возможность обойти обязательное правило.

## Неотменяемые kernel protocols

- trust boundaries, local process ownership и secret handling;
- правила включённых trusted capability profiles, включая memory authoring contract;
- compaction threshold и pinned context;
- tool schema validation;
- delegation contract, minimum sufficient tool/skill allowlists, child budgets и bounded autonomy: точный outcome/scope при свободном выборе child-ом метода внутри выданных границ;
- если следующий delegation level запрещён, явное указание работать без новых сабагентов при отсутствии `core_delegate` в model catalog;
- background task lifecycle, cancellation, notifications и passive wait;
- durable checkpoints и защита от duplicate side effects;
- audit, redaction и OpenTelemetry instrumentation;
- запрет раскрытия raw chain-of-thought.
- provider-wire aliases инструментов являются transport-only: в пользовательском тексте агент использует канонические имена из descriptions и не приписывает aliases продуктовую семантику.

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

User Message передаётся модели отдельной user-role записью и MUST NOT копироваться в system instructions. AgentProfilePrompt является optional: при отсутствии явной роли его default пуст и segment не добавляется.

## Выбор инструментов

Пользователь не обязан называть tool явно. Agent выбирает capability по смыслу задачи, но не вызывает tool, который не улучшает корректность результата. Stable knowledge и pure language work выполняются напрямую; runtime-dependent, accuracy-sensitive, durable или stateful result проверяется через самый узкий доступный authoritative tool. Если подходящей capability нет, agent сообщает, что значение не удалось проверить, а не выдумывает его.

Tool-specific lifecycle не дублируется целиком в base kernel: он находится в conditional capability policy и model-facing description зарегистрированного tool. Отключённая capability не добавляет свой policy text.

## AgentProfilePrompt

AgentProfilePrompt MAY задавать:

- роль, tone и доменную специализацию;
- критерии качества и формат результата;
- guidance по выбору переданных MCP/skills;
- предпочтительную стратегию исследования или коммуникации;
- дополнительные консервативные ограничения.

Платформа MAY оставить AgentProfilePrompt пустым. Generic instruction вроде `Complete the user's task using available tools.` не является обязательным default: цель уже приходит отдельным user Message.

Он MUST NOT:

- добавлять tool/skill/MCP, отсутствующий в capability snapshot;
- адресовать чужую TerminalSession или расширять назначенные workspace/network capabilities;
- менять memory authoring contract, если memory включена;
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
