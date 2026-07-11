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
- `core.delegate` — создать child-agent Task по строгому delegation contract.

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
  "budget": {"turns": 20, "tool_calls": 40},
  "result_schema": "artifact://schemas/review-result.json"
}
```

Требования:

- `instruction` содержит одну узкую цель, ограничения и success criteria;
- `tools`, `skills` и server-scoped `mcp` являются allowlists, а не рекомендациями;
- каждый элемент MUST входить в capability set parent-а;
- child не видит остальные рабочие tools/skills даже на discovery;
- protocol-internal lifecycle, audit и safe completion сохраняются runtime-ом, но model-callable tools определяются EffectiveConfig и delegation allowlist; parent не может передать отключённую capability;
- budget является частью parent budget и не увеличивается child-ом;
- result имеет schema, provenance и перечисляет непроверенные assumptions.

Если parent не перечислил необходимую capability, child возвращает `blocked`/`input-required`; он не расширяет allowlist самостоятельно.

## Общая память

Core Agent не имеет собственной общей memory. Main и child разделяют память, только если parent явно перечислил в delegation contract тот же Memory MCP server и namespace:

- child получает только перечисленные memory tools;
- чтение видит committed revision Memory Service;
- запись использует expected file/repository revision;
- successful commit запускает indexing/NER внутри Memory Service;
- service revision notification становится доступна parent через MCP/event bridge;
- conflict не разрешается last-write-wins;
- если Memory MCP не передан или AgentConfig memory disabled, child работает без memory.

Working scratchpad и незавершённый model context не являются общей памятью. Для передачи результата child публикует Artifact или committed memory change.

В этой спецификации «сабагент» означает managed child Core Agent Task. Произвольный внешний opaque A2A peer не получает Memory MCP credentials автоматически: parent передаёт ему только явно выбранные Message Parts/Artifacts и принимает результат как недоверенный внешний input.

## Execution isolation сабагента

Каждый child-agent Task получает отдельный [ExecutionEnvironment](execution-environment.md). Он не наследует процессы, shell sessions, writable rootfs или environment variables parent-а. Общий проект передаётся snapshot/volume с copy-on-write overlay; изменения возвращаются patch/artifact и сливаются управляемо.

## Ожидание человека

Child MAY перейти в `input-required`, но запрос маршрутизируется через parent/task coordinator и A2A status. Child не обходит parent и не отправляет внешнее сообщение самостоятельно, если это не было явно делегированной capability.

## Cancellation и завершение

- Cancel parent рекурсивно запрашивает cancel children, кроме явно detached durable tasks с owner/orphan policy.
- Task cancellation кооперативна до grace period, затем executor принудительно завершает environment.
- Parent MUST проверить terminal status и required artifacts до использования результата.
- Child failure не обязан завершать parent: модель получает structured failure и выбирает fallback.
- Parent не может объявить итог, зависящий от pending task, не отметив результат как незавершённый.
