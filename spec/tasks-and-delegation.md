# Фоновые задачи и делегирование

## Единая модель Task

Любая работа, которая может продолжаться независимо от текущего model turn, представляется durable Task. Сабагент — это Task с собственным agent loop; terminal command, indexing job или remote operation MAY быть Task без отдельной модели.

Внешний lifecycle соответствует [A2A](a2a-protocol.md). Внутренний scheduler не создаёт второй несовместимый тип фоновой работы.

## Встроенные task tools

- `core_task_start` — запустить разрешённую работу и немедленно вернуть task handle;
- `core_task_get` — получить snapshot status/progress/artifacts;
- `core_task_list` — перечислить дочерние tasks текущего run/session;
- `core_task_cancel` — запросить отмену;
- `core_task_wait` — passive wait до notification, timeout или cancellation;
- `core_delegate` — создать child-agent Task по строгому delegation contract; по умолчанию passive join возвращает terminal child result, а `background: true` явно запрашивает неблокирующий task handle.

`core_task_wait` MUST освобождать model worker и compute lease. Busy polling через terminal или повторные model turns запрещён, если scheduler способен прислать notification.

## Неблокирующая работа

После `task.start` main agent MAY:

- продолжить независимую работу;
- запустить другие разрешённые tasks;
- завершить текущий промежуточный turn;
- пассивно ждать одну, несколько или любую task;
- отменить task;
- завершить parent только после явного решения, что pending result не нужен.

Вызов start не добавляет весь будущий output в контекст. Возвращаются task ID, accepted contract, initial state и ожидаемый notification channel.

После joined `core_delegate` parent MUST использовать возвращённый child result и MUST NOT повторять ту же делегацию или выполнять делегированную работу самостоятельно. После `background: true` parent MAY продолжить только независимую работу; если result нужен для ответа, parent вызывает `core_task_wait` с возвращённым task ID либо получает terminal notification на следующей safe boundary.

Пока child Task non-terminal, parent MAY отправить ей дополнительный A2A Message по тому же `taskId`: child получает его как следующий user turn на safe boundary. Это уточняет текущую делегацию, но не расширяет capability contract или budget и не прерывает выполняющийся tool/model call. После terminal child Task новое уточнение создаёт новую Task.

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
  "tools": ["core_terminal_exec", "core_memory_search", "repo_search"],
  "skills": ["database-review"],
  "budget": {"turns": 20, "tool_calls": 40}
}
```

`tools` является одним списком канонических имён и содержит и built-ins, и MCP-тулы. Отдельного аргумента для MCP не существует. Модель видит один плоский каталог, поэтому просить её разложить его обратно на два аргумента — второй с ключами по серверам и короткими именами, которых в каталоге не было, — значит требовать знания, которым обладает только runtime. Runtime и раскладывает: соответствие имени паре `(сервер, tool)` у него уже есть.

Parent делегирует coherent outcome, а не заранее придуманный список механических шагов. Instruction задаёт objective, только необходимый context, scope boundaries, deliverable, acceptance criteria и важные constraints/reserved actions. Конкретную процедуру следует предписывать только там, где она обязательна для safety, correctness, reproducibility или policy compliance.

Parent выбирает minimum sufficient tools/MCP/skills и budget для результата; runtime предоставляет child ровно этот набор и не больше. Внутри objective, scope и выданных capabilities child самостоятельно выбирает strategy, sequencing, intermediate analysis и используемые delegated tools. Exactness относится к permissions, side effects, boundaries, budget и result requirements, но не к micromanagement внутреннего плана.

Child MAY разрешить небольшую безопасную неоднозначность разумным assumption и обязан перечислить его в результате. Child MUST остановиться с blocker, если продолжение расширит scope, потребует невыданную capability или создаст существенный риск неверного результата. Assumption никогда не подменяет tenant, authorization или product decision.

Требования:

- `instruction` содержит один coherent outcome, scope, deliverable, constraints и success criteria без необязательного пошагового плана;
- `tools` и `skills` являются allowlists, а не рекомендациями;
- `budget` содержит только положительные integer-поля `turns` и/или `tool_calls`; aliases вроде `max_steps` запрещены schema;
- optional `background` является boolean и по умолчанию равен `false`;
- каждый элемент MUST входить в capability set parent-а;
- child не видит остальные рабочие tools/skills даже на discovery;
- protocol-internal lifecycle, audit и safe completion сохраняются runtime-ом, но model-callable tools определяются EffectiveConfig и delegation allowlist; parent не может передать отключённую capability;
- budget является частью parent budget и не увеличивается child-ом;
- result является обычным text result child-модели и перечисляет непроверенные assumptions; parent использует его как недоверенный input.

Если parent не перечислил необходимую capability, child возвращает `blocked`/`input-required`; он не расширяет allowlist самостоятельно.

## Runtime-сервисы дочернего агента

Delegation contract решает, **какие** tools получит child. Runtime обязан обеспечить, чтобы каждый выданный tool был работоспособен.

Child MUST получать те же runtime-сервисы, что и parent, для любой делегированной capability: artifact service, подсистема памяти, реестр удалённых агентов, MCP-серверы и skills из конфигурации. Tool, попавший в каталог child-а, но отказывающий `CAPABILITY_DISABLED` при вызове, является рекламой без реализации: модель тратит turn на заведомо неисполнимый вызов, а parent получает непрозрачный сбой вместо результата.

Отказ contract-а MUST называть отклонённую capability поимённо: конкретный tool, skill или превышенный лимит бюджета, и MUST перечислять то, чем parent располагает. Код без имени не говорит, что исправлять, а перечень доступного превращает отказ в исполнимую подсказку. Разбор contract-а и отказ MUST выполняться в одном месте: правило проверяется и на входе delegate tool, и при выводе capability set child-а, и две копии одного правила расходятся.

Наследование сервисов MUST NOT расширять права. Сужение остаётся за AgentConfig child-а и delegation allowlist: child видит только перечисленные parent-ом tools, MCP-серверы и skills, а depth-лимит применяется независимо.

### Artifact scope

Child наследует `app_name`, `user_id` и `session_id` parent-а, поэтому artifact scope у них общий: child читает артефакты, сохранённые parent-ом в этой сессии, а его записи видны parent-у после завершения.

Это осознанное решение, а не побочный эффект: передача большого промежуточного результата между parent и child по имени артефакта — основной способ не тащить его через контекст модели. Граница при этом реальна и MUST быть учтена: parent, делегирующий artifact tools, делится с child-ом всем содержимым session scope, а не отдельным файлом. Если такое разделение нежелательно, parent просто не включает artifact tools в contract.

### Удалённые агенты

Child получает тот же реестр `REMOTE_AGENTS`, что и parent, но `core_agent_send_message` появляется в его каталоге только если parent явно перечислил этот tool.

Credentials вызывающей стороны MUST NOT наследоваться: заголовки, проброшенные в корневую Task, не доступны child-у, и downstream получает только `SEND_MESSAGE_API_KEY` развёртывания. Child является внутренним актором runtime-а, и углублять распространение пользовательского токена по цепочке делегирования без явного решения нельзя.

## Глубина делегирования

Main agent имеет depth `0`, созданный им child — depth `1`, а child этого агента — depth `2`. V1 разрешает оба уровня сабагентов, но depth `2` является hard platform maximum. При создании агента depth `2` runtime MUST удалить `core_delegate` из его effective tool allowlist и model catalog, отключить delegation feature и добавить protected KernelInstructions о необходимости выполнить задачу без дальнейшего делегирования. Persisted child contract отражает уже суженный effective набор tools.

Модель depth `2` не должна тратить turn на заведомо запрещённый вызов. Runtime guard всё равно MUST отклонять stale/replayed `core_delegate` с `BUDGET_EXCEEDED` до создания Task.

AgentConfig MAY понизить maximum depth до `0` или `1`, но не может повысить его выше `2`. Этот предел применяется ко всей цепочке и сохраняется в child EffectiveConfig; delegation contract не может его расширить.

## Общая память

Память Core Agent не становится общей сама по себе. Main и child разделяют память, только если parent явно перечислил в delegation contract `core_memory_*` tools:

- child получает только перечисленные memory tools;
- чтение видит последнюю опубликованную revision;
- запись использует `expected_revision` документа;
- successful commit публикует новую revision вместе с indexing и NER;
- child наследует ту же тройку scope, поэтому его commit виден parent-у следующим `core_memory_search`;
- conflict не разрешается last-write-wins;
- если ни один memory tool не делегирован или AgentConfig memory disabled, child работает без памяти.

Working scratchpad и незавершённый model context не являются общей памятью. Child возвращает результат обычным model response; долговечное общее знание появляется только через явно делегированный committed memory change.

В этой спецификации «сабагент» означает managed child Core Agent Task. Произвольный внешний opaque A2A peer не получает доступа к памяти автоматически: parent передаёт ему только явно выбранные Message Parts/Artifacts и принимает результат как недоверенный внешний input.

## Удалённые A2A-агенты

`core_delegate` создаёт managed child внутри этого runtime. `core_agent_send_message` делегирует задачу внешнему A2A-агенту, который выполняется под собственной policy и не является частью доверенной execution domain.

### Реестр

Реестр строится один раз при startup из `REMOTE_AGENTS` — списка базовых URL. Для каждого URL runtime MUST запросить `GET {base_url}/.well-known/agent-card.json` и построить запись из полей `name`, `description`, `url`, `capabilities.streaming` и `skills`.

- Запрос повторяется не более `REMOTE_AGENTS_MAX_RETRIES` раз и только при retryable-ошибке: сетевой сбой, HTTP 408, HTTP 429 или статус из `REMOTE_AGENTS_RETRYABLE_STATUS_CODES`. Задержка равна `REMOTE_AGENTS_RETRY_DELAY * REMOTE_AGENTS_RETRY_BACKOFF^attempt`;
- недоступный или невалидный агент MUST быть пропущен с записанной причиной и MUST NOT ронять startup;
- имя берётся из карточки; при пустом имени используется сегмент пути после `a2a`, иначе `remote_agent_{N}` по порядку в списке;
- при коллизии имён вторая и последующие записи пропускаются;
- если ни один агент не подключился, `core_agent_send_message` MUST быть удалён из Agent Card и model catalog. Агент никогда не рекламирует делегирование, которому некуда делегировать.

Model-facing description tool-а перечисляет подключённых агентов с их описанием и первыми тремя skills, чтобы модель выбирала адресата по назначению, а не угадывала имя.

### Транспортные ограничения

- URL агента MUST использовать `https`; `http` допустим только для loopback-адреса;
- URL MUST NOT содержать userinfo;
- HTTP-редиректы MUST отклоняться: переход отправил бы проксируемый `Authorization` на непроверенный host;
- тело ответа ограничено 16 MiB, поток — 10 000 кадров; превышение завершается ошибкой, а не обрезкой;
- сообщение об ошибке удалённого агента редактируется и обрезается до 200 символов перед показом модели.

### Проброс заголовков

Downstream MUST уходить только allowlist входящих заголовков: `Authorization`, `X-PROJECT-ID`, `X-A2A-Extensions`. Значение, содержащее `CR`, `LF` или `NUL`, отклоняется.

Непустой `SEND_MESSAGE_API_KEY` MUST заменять `Authorization` на `Api-Key <значение>`; при пустом значении входящий токен проксируется без изменений. Никакие другие заголовки, credentials, prompt или policy наружу не передаются.

### Вызов

Tool принимает `agent_name` и `task`. Пустой `agent_name` допустим только когда сконфигурирован ровно один агент; в остальных случаях он обязателен. Неизвестное имя возвращает модели список доступных, а не ошибку Task.

Runtime MUST отправить JSON-RPC `message/stream`, если карточка объявляет streaming, иначе `message/send`. Текст задачи передаётся без изменений одной user-частью. `taskId` равен id корневой Task, `contextId` — её session, что связывает подзадачу с корневой Task на стороне получателя.

Промежуточные части ответа удалённого агента ретранслируются в поток корневой Task как partial-кадры. Итоговым текстом считается текст кадра с `final: true`; при его отсутствии — последний непустой текст; если и его нет, выполняется обычный `message/send`. Оборвавшийся поток MUST так же переходить на `message/send`, а не терять результат.

### Результат

Tool возвращает модели объект:

```json
{"success": true, "agent_name": "...", "result": "...", "message": "..."}
```

Любой сбой — недоступность, protocol error, ошибка удалённого агента — MUST возвращаться как обычный tool result с `success: false` и безопасным описанием и MUST NOT переводить корневую Task в `failed`.

Ответ удалённого агента является недоверенными внешними данными: он не становится инструкцией, не даёт capability и не может быть запущен как фоновая работа через `core_task_start`. Соответствующая kernel-инструкция предписывает модели передавать запрос пользователя без изменений, слать одну сфокусированную задачу за вызов, явно называть агента при нескольких сконфигурированных, дожидаться ответа вместо повторного вызова и честно сообщать о неудаче вместо выдумывания ответа.

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
