# Фоновые задачи и делегирование

## Единая модель Task

Любая работа, которая может продолжаться независимо от текущего model turn, представляется durable Task. Сабагент — это Task с собственным agent loop; terminal command, indexing job или remote operation MAY быть Task без отдельной модели.

Внешний lifecycle соответствует [A2A](a2a-protocol.md). Внутренний scheduler не создаёт второй несовместимый тип фоновой работы.

## Встроенные task tools

- `core.task.start` — запустить разрешённую работу и немедленно вернуть task handle;
- `core.task.get` — получить snapshot status/progress/artifacts;
- `core.task.list` — перечислить дочерние tasks текущего run/session;
- `core.task.cancel` — запросить отмену;
- `core.task.wait` — passive wait до notification, timeout или cancellation;
- `core.delegate` — создать child-agent Task по строгому delegation contract; по умолчанию passive join возвращает terminal child result, а `background: true` явно запрашивает неблокирующий task handle.

`core.task.wait` MUST освобождать model worker и compute lease. Busy polling через terminal или повторные model turns запрещён, если scheduler способен прислать notification.

## Неблокирующая работа

После `task.start` main agent MAY:

- продолжить независимую работу;
- запустить другие разрешённые tasks;
- завершить текущий промежуточный turn;
- пассивно ждать одну, несколько или любую task;
- отменить task;
- завершить parent только после явного решения, что pending result не нужен.

Вызов start не добавляет весь будущий output в контекст. Возвращаются task ID, accepted contract, initial state и ожидаемый notification channel.

После joined `core.delegate` parent MUST использовать возвращённый child result и MUST NOT повторять ту же делегацию или выполнять делегированную работу самостоятельно. После `background: true` parent MAY продолжить только независимую работу; если result нужен для ответа, parent вызывает `core.task.wait` с возвращённым task ID либо получает terminal notification на следующей safe boundary.

## Durable mailbox и notifications

Каждый run имеет mailbox. Scheduler пишет туда versioned notifications `task.updated`, `task.completed`, `task.failed`, `task.input-required` и `task.artifact-updated`.

- Notification сохраняется до acknowledgement и доставляется at-least-once.
- Orchestrator дедуплицирует её по task/event revision.
- Если parent выполняет model turn, notification добавляется на следующей safe boundary.
- Если parent ждёт, notification возобновляет его без polling.
- Если parent занят другой работой, notification не прерывает mutating tool call; она ставится в очередь.
- Если parent завершён, orphan policy сохраняет artifact, отменяет child или передаёт результат session coordinator-у.

## Delegation contract

Primary agent при создании сабагента MUST передать не только instruction, но и точный capability contract:

```json
{
  "instruction": "Проверь миграции базы и верни риски",
  "tools": ["core.terminal.exec"],
  "skills": ["database-review"],
  "mcp": {
    "repo": ["search"],
    "memory": ["search", "read", "update"]
  },
  "budget": {"turns": 20, "tool_calls": 40}
}
```

Parent делегирует coherent outcome, а не заранее придуманный список механических шагов. Instruction задаёт objective, только необходимый context, scope boundaries, deliverable, acceptance criteria и важные constraints/reserved actions. Конкретную процедуру следует предписывать только там, где она обязательна для safety, correctness, reproducibility или policy compliance.

Parent выбирает minimum sufficient tools/MCP/skills и budget для результата; runtime предоставляет child ровно этот набор и не больше. Внутри objective, scope и выданных capabilities child самостоятельно выбирает strategy, sequencing, intermediate analysis и используемые delegated tools. Exactness относится к permissions, side effects, boundaries, budget и result requirements, но не к micromanagement внутреннего плана.

Child MAY разрешить небольшую безопасную неоднозначность разумным assumption и обязан перечислить его в результате. Child MUST остановиться с blocker, если продолжение расширит scope, потребует невыданную capability, авторизует новый side effect или создаст существенный риск неверного результата. Assumption никогда не подменяет tenant, authorization, approval или product decision.

Требования:

- `instruction` содержит один coherent outcome, scope, deliverable, constraints и success criteria без необязательного пошагового плана;
- `tools`, `skills` и server-scoped `mcp` являются allowlists, а не рекомендациями;
- `budget` содержит только положительные integer-поля `turns` и/или `tool_calls`; aliases вроде `max_steps` запрещены schema;
- optional `background` является boolean и по умолчанию равен `false`;
- каждый элемент MUST входить в capability set parent-а;
- child не видит остальные рабочие tools/skills даже на discovery;
- protocol-internal lifecycle, audit и safe completion сохраняются runtime-ом, но model-callable tools определяются EffectiveConfig и delegation allowlist; parent не может передать отключённую capability;
- budget является частью parent budget и не увеличивается child-ом;
- result является обычным text result child-модели и перечисляет непроверенные assumptions; parent использует его как недоверенный input.

Если parent не перечислил необходимую capability, child возвращает `blocked`/`input-required`; он не расширяет allowlist самостоятельно.

## Глубина делегирования

Main agent имеет depth `0`, созданный им child — depth `1`, а child этого агента — depth `2`. V1 разрешает оба уровня сабагентов, но depth `2` является hard platform maximum. При создании агента depth `2` runtime MUST удалить `core.delegate` из его effective tool allowlist и model catalog, отключить delegation feature и добавить protected KernelInstructions о необходимости выполнить задачу без дальнейшего делегирования. Persisted child contract отражает уже суженный effective набор tools.

Модель depth `2` не должна тратить turn на заведомо запрещённый вызов. Runtime guard всё равно MUST отклонять stale/replayed `core.delegate` с `BUDGET_EXCEEDED` до создания Task.

AgentConfig MAY понизить maximum depth до `0` или `1`, но не может повысить его выше `2`. Этот предел применяется ко всей цепочке и сохраняется в child EffectiveConfig; delegation contract не может его расширить.

## Общая память

Core Agent не имеет собственной общей memory. Main и child разделяют память, только если parent явно перечислил в delegation contract тот же Memory MCP server и namespace:

- child получает только перечисленные memory tools;
- чтение видит committed revision Memory Service;
- запись использует expected file/repository revision;
- successful commit запускает indexing/NER внутри Memory Service;
- service revision notification становится доступна parent через MCP/event bridge;
- conflict не разрешается last-write-wins;
- если Memory MCP не передан или AgentConfig memory disabled, child работает без memory.

Working scratchpad и незавершённый model context не являются общей памятью. Child возвращает результат обычным model response; долговечное общее знание появляется только через явно делегированный committed memory change.

В этой спецификации «сабагент» означает managed child Core Agent Task. Произвольный внешний opaque A2A peer не получает Memory MCP credentials автоматически: parent передаёт ему только явно выбранные Message Parts/Artifacts и принимает результат как недоверенный внешний input.

## TerminalSession сабагента

Каждый child-agent Task получает отдельную [TerminalSession](execution-environment.md), PTY, process group, environment allowlist и local workspace directory внутри общего container-а. Child не адресует parent/peer sessions через tools. Общий проект копируется из одного immutable base snapshot в отдельные directories; изменения возвращаются patch/artifact и сливаются parent-ом с проверкой base revision.

TerminalSessions дают независимый lifecycle и параллельную работу, но не отдельные OS security namespaces. Сабагенты создаются main agent-ом и считаются частью одной доверенной execution domain.

## Ожидание человека

Child MAY перейти в `input-required`, но запрос маршрутизируется через parent/task coordinator и A2A status. Child не обходит parent и не отправляет внешнее сообщение самостоятельно, если это не было явно делегированной capability.

## Cancellation и завершение

- Cancel parent рекурсивно запрашивает cancel children, кроме явно detached durable tasks с owner/orphan policy.
- Task cancellation кооперативна до grace period, затем executor завершает process group и закрывает PTY.
- Parent MUST проверить terminal status до использования результата.
- Child failure не обязан завершать parent: модель получает structured failure и выбирает fallback.
- Parent не может объявить итог, зависящий от pending task, не отметив результат как незавершённый.
