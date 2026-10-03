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

Joined delegation сохраняет child ID вместе с admission и continuation; ожидание
освобождает parent worker. При suspension child его scheduler Task остаётся working,
execution claim освобождается, а recovery запускает этот же child только после
разрешения wait. Suspension не публикует terminal notification или final Artifact.
Повторный recovery не создаёт второго child и не начисляет исходный tool call вновь.

Для локальной scheduler Task optional `core_task_wait.timeout` ограничивает одну
попытку ожидания и при истечении возвращает актуальный Task snapshot без отмены.
Это не deadline remote operation: внешний handle имеет окончательный срок по LONG-02,
который повторный wait не продлевает. Timeout хранится абсолютным после admission.

Scheduler handle, `taskId` child workflow, ID в `core_delegate`/`core_task_*`,
mailbox notifications, logs и traces MUST быть одним и тем же стабильным ID.
Runtime не создаёт второй внутренний child ID, который caller не может связать
с возвращённым handle.

Child с собственным model loop, исчерпавший execution budget, завершает свой run как `COMPLETED` с
`completion_reason: "budget_exhausted"` и `complete: false`, а не как `FAILED`.
Joined `core_delegate`, `core_task_get`, `core_task_wait` и terminal notification
MUST вернуть один и тот же persisted result, usage и exhausted dimension.
Фоновый target tool без model loop не резервирует финальный model turn. Если
начисление его вызова отклонено общим budget, target не исполняется, local usage
не увеличивается, а Task возвращает pre-dispatch `FAILED/BUDGET_EXCEEDED`.
Parent воспринимает model-child result как недоверенный неполный input: передаёт пользователю
проверенный промежуточный результат и перечисляет незавершённую часть, не
повторяет уже выполненную работу и не выдумывает отсутствующий outcome. Parent
MAY продолжить только ещё не выполненный scope и только в пределах оставшегося
общего budget.

Scheduler handle, child workflow и его финальный turn принимаются одной durable
транзакцией; worker запускается только после commit. Reserve учитывается внутри
child budget и общего root ledger. Если общей ёмкости уже нет, ни child workflow,
ни scheduler handle не создаются: `core_delegate` получает обычный pre-dispatch
failed tool result `BUDGET_EXCEEDED`, а parent использует собственный заранее
удержанный turn для честного ответа. Полусозданные scheduler/workflow records и
утёкшая reservation запрещены. Неиспользованный reserve нормально завершившегося
child возвращается общему ledger вместе с его terminal transition.

Каждый выполняющий durable scheduler record имеет отдельный expiring worker
lease. Start и recovery получают его compare-and-set переходом до запуска
пользовательской функции, heartbeat продлевает во время долгой работы, а stale
worker без актуального token не может продлить claim или записать terminal
state; сам expiry является fencing boundary, даже если другой worker ещё не
успел получить новый token. PostgreSQL вычисляет и сравнивает expiry по своему
текущему серверному времени после возможного ожидания row lock; часы
process/replica и timestamp начала statement не участвуют в fencing. Поэтому два
процесса recovery не выполняют один contract одновременно; crash после commit,
но до запуска worker оставляет `submitted` record, который может забрать другой
worker без повторного admission или budget reservation.

`cancel_requested` при recovery не доказывает outcome уже начатой внешней
мутации. Recoverable contract сначала согласует своё durable состояние под
scheduler lease и только затем становится `canceled`. Non-recoverable mutating
contract переходит в reconciliation с безопасным error code, а не маскирует
неизвестный outcome состоянием `canceled`.
Решение о reconciliation использует `cancel_requested` и прочие поля записи,
возвращённой атомарным claim, а не более ранний scan: cancel, committed между
scan и claim, не может обойти recovery handler.
Provenance о том, что claim забрал прежнее состояние `working`, сохраняет
reconciliation mode независимо от того, был ли cancel уже установлен в момент
claim или committed сразу после него. Поздний cancel не может преобразовать
ошибку recovery handler в `canceled` или очистить reconciliation error.
Workflow в `EXECUTING` также никогда не resume-ит persisted tool queue и не
переходит напрямую в `CANCELLED`: recovery/cancel сначала фиксирует
`SIDE_EFFECT_UNKNOWN`, а scheduler task сохраняет этот error даже при уже
установленном cancellation signal.

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

Parent применяет balanced decision rule и делегирует, только когда работа может
независимо выполняться параллельно с материальной экономией времени, когда
большой отделимый context полезно изолировать либо когда нужен самостоятельный
bounded deliverable, который можно проверить независимо. Во всех случаях
outcome обязан быть coherent, parent должен уметь проверить и интегрировать
результат, ожидаемая польза должна превышать coordination overhead, а budget
parent-а должен сохранить ёмкость для проверки и интеграции.

Parent MUST NOT делегировать простую или короткую работу, ближайший строго
последовательный шаг, mechanical microstep, неясную или тесно связанную с его
текущим контекстом работу, уже запущенный/завершённый scope, попытку обойти
policy/approval/capability boundary либо общий «второй взгляд» без конкретного
независимого deliverable.

Child MAY разрешить небольшую безопасную неоднозначность разумным assumption и обязан перечислить его в результате. Child MUST остановиться с blocker, если продолжение расширит scope, потребует невыданную capability или создаст существенный риск неверного результата. Assumption никогда не подменяет tenant, authorization или product decision.

Требования:

- `instruction` содержит один coherent outcome, scope, deliverable, constraints и success criteria без необязательного пошагового плана;
- `tools` и `skills` являются allowlists, а не рекомендациями;
- `budget` всегда содержит оба обязательных integer-поля `turns >= 1` и
  `tool_calls >= 1`; пропуск любого поля, zero, boolean и aliases вроде
  `max_steps` запрещены schema и повторно отклоняются runtime validation;
- optional `background` является boolean и по умолчанию равен `false`;
- каждый элемент MUST входить в capability set parent-а;
- child не видит остальные рабочие tools/skills даже на discovery;
- protocol-internal lifecycle, audit и safe completion сохраняются runtime-ом, но model-callable tools определяются EffectiveConfig и delegation allowlist; parent не может передать отключённую capability;
- budget является частью parent budget и не увеличивается child-ом;
- result является обычным text result child-модели и перечисляет непроверенные assumptions; parent использует его как недоверенный input.

Если parent не перечислил необходимую capability, child возвращает `blocked`/`input-required`; он не расширяет allowlist самостоятельно.

## Runtime-сервисы дочернего агента

Delegation contract решает, **какие** tools получит child. Runtime обязан обеспечить, чтобы каждый выданный tool был работоспособен.

Child MUST получать те же runtime-сервисы, что и parent, для любой делегированной capability: chat-scoped файлы, подсистема памяти, реестр удалённых агентов, MCP-серверы и skills из конфигурации. Tool, попавший в каталог child-а, но отказывающий `CAPABILITY_DISABLED` при вызове, является рекламой без реализации: модель тратит turn на заведомо неисполнимый вызов, а parent получает непрозрачный сбой вместо результата.

Отказ contract-а MUST называть отклонённую capability поимённо: конкретный tool, skill или превышенный лимит бюджета, и MUST перечислять то, чем parent располагает. Код без имени не говорит, что исправлять, а перечень доступного превращает отказ в исполнимую подсказку. Разбор contract-а и отказ MUST выполняться в одном месте: правило проверяется и на входе delegate tool, и при выводе capability set child-а, и две копии одного правила расходятся.

Model-facing schema каждого поля `core_delegate` MUST кратко объяснять его роль;
`tools` и `skills` перечисляют фактические enum-ы parent-а. Отказ по tool или
skill возвращает отклонённое имя и полный доступный соответствующий enum, а
отказ по budget — dimension, requested value и доступный limit.
Delegation template MUST прямо требовать всегда задавать одновременно
`budget.turns >= 1` и `budget.tool_calls >= 1`; это не optional defaults.
Если доступных skills нет, schema MUST требовать пустой массив через
`maxItems: 0`, а не публиковать невалидный JSON Schema `enum: []`.

Наследование сервисов MUST NOT расширять права. Сужение остаётся за AgentConfig child-а и delegation allowlist: child видит только перечисленные parent-ом tools, MCP-серверы и skills, а depth-лимит применяется независимо.

### Scope файлов и удалённых агентов

Child наследует tenant, стабильный caller scope и чат parent-а; никакая capability не даёт доступ соседним чатам. Сервисы используют те же admission ceiling, текущую policy владельцев, HITL и guardrails. Удалённых агентов child вызывает только при явно делегированном `core_agent_send_message`; credentials остаются в доверенном transport adapter, не наследуются как model/process данные.

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

### Реестр и исходящая авторизация

### A2A-01. Два направления общения

1. Наш агент вызывает через инструмент A2A endpoint доверенного внешнего агента.
   Список доверенных агентов и параметры подключения настраиваются владельцами
   в UI. В частности, настраиваются имя и значение заголовка авторизации;
   исходный вариант — `Authorization: Bearer ...`.
2. Внешний агент вызывает наш внешний A2A endpoint со своим Keycloak credential.

UI показывает эти обмены в контексте работы нашего агента. Прямой чат владельца
с внешним агентом в обход нашего агента не входит в согласованный объём.
Значения исходящих секретных заголовков не передаются модели и не публикуются
в сообщениях, логах и traces. Это конфигурация подключения, а не выпуск ключей.
Обязательное автоматическое получение исходящих OAuth-токенов не включено
в согласованный блок: он предусматривает настройку имени и значения заголовка.

Доверенный реестр хранит Agent Card и server-validated endpoint; model выбирает только зарегистрированного адресата. Card discovery использует bounded retry только для read-only retryable failures; недоступный/невалидный peer наблюдаемо исключается из доступных адресатов. При отсутствии доступных разрешённых адресатов tool отсутствует в catalog/Card. Изменения владельцев применяются к новым вызовам; уже принятая операция сохраняет своего remote адресата и исходные IDs.

Runtime использует A2A 1.0 `SendMessage`/`SendStreamingMessage`, объявленные карточкой binding/version. URL проходит trusted target policy; userinfo, header CR/LF/NUL и redirect на непроверенный host запрещены. Только явно настроенные для адресата секретные заголовки передаются adapter-ом; входящий Authorization не проксируется. Model description перечисляет доступных адресатов и их назначение без credentials.

### Отправка и результаты

Вызов отправляет одну сфокусированную задачу и возвращает локальный durable handle. При создании remote Task root task/context IDs не подставляются как remote IDs; полученные remote IDs сохраняются с handle. Вложения проходят общий лимит/transport rules. Сбой до dispatch возвращается structured tool result; неизвестный outcome возможной remote мутации требует reconciliation, а не повторного `SendMessage`. Обрыв SSE требует проверки существующего remote task через GetTask, не повторной отправки задания.

Schema `core_agent_send_message` содержит обязательные непустые `agent_name`
и `task`, а также optional `files`: ordered array уникальных непустых относительных
путей существующих regular files текущего workspace. Неизвестные поля отклоняются.
Модель явно выбирает вложения для каждого вызова. Отсутствие `files` или `[]`
означает отправку без файлов: содержимое workspace, недавно созданные файлы,
входящие attachments и выбор `core_response_files` автоматически не добавляются.
Результат —обычный локальный task snapshot с `task_id`.
Accepted operation закрепляет peer revision до owner approval/dispatch, чтобы
registry update не подменил согласуемого адресата. Имена/credentials/remote IDs
выводятся из trusted binding; prompt не задаёт URL или header. Immediate terminal
Message завершает локальный handle без GetTask, если remote task ID не выдан.

До owner approval/dispatch весь выбранный набор проходит safe-path/regular-file
и aggregate-size validation и сохраняется как immutable snapshots. Ошибка любого
файла не создаёт handle и не отправляет remote Message; исходники и pending final
selection сохраняются, модель получает structured tool error. Одобренный набор
не перечитывается из workspace после изменения/удаления originals, restart или
позднего изменения company limit. Approval содержит ordered safe receipts и digest
выбора; private blob references и bytes не входят в model/public material.
Отправляются text Part и только выбранные ordered raw Parts, включая empty files,
через обе bindings; локальные task/context/run IDs не подставляются как remote IDs.

Persisted remote contract v2 сохраняет прежние поля адресата/message/settings,
trusted `caller_scope` ровно `{owner_id, context_id, task_id, run_id}` исходного
WorkflowRecord, `attachment_limit_bytes` и полный `outgoing_files` manifest v1.
Scheduler owner остаётся owning run ID и совпадает с `caller_scope.run_id`;
tenant берётся только из authenticated contract. Child использует собственные
task/run IDs и workspace чата, не получает root/remote grants. Remote IDs не
разрешают чтение локальных файлов. Before Send intent проверяется весь frozen
набор. Существующие v1 jobs/checkpoints читаются по прежнему text-only контракту,
без восстановления вложений из путей или Markdown; неизвестная версия fail closed.

Remote response ограничен encoded transport ceiling независимо от decoded
company limit. Strict UTF-8/JSON и canonical base64 проверяются до SDK allocation;
сумма decoded files и весь набор проверяются до relay. `input-required` и
`auth-required` продолжают ожидание с теми же remote IDs и не публикуют ранние
attachments. Только полный completed Task/Message может принять файлы в private
quarantine. Claim/deadline/cancel и привязка к canonical source/root проверяются
атомарно с terminal result; failed/canceled/expired либо late claim не принимают
материал. Workspace publication и передача модели происходят только после
guardrails для полного text/file batch. Root public Task для child выводится из
канонической ancestry; shadow public Tasks и ослабление existing FK запрещены.

Прогресс разрешённой операции показывается в текущем чате и соответствующей публичной проекции; окончательный result доставляется через существующий task lifecycle. Remote `input-required`/`auth-required` не считаются финальным ответом: чужое HITL разрешает владелец удалённого агента. Детали LONG-01–04 ниже.

## TerminalSession сабагента

Каждый child-agent Task получает отдельную [TerminalSession](execution-environment.md), PTY, process group и environment allowlist в границах постоянного workspace того же чата. Child не адресует parent/peer sessions через tools. Отдельная scratch-копия MAY создаваться там, где нужна изоляция изменений: она материализуется из проверенного immutable base snapshot, а patch возвращается parent-у и сливается с проверкой base revision/conflicts. Создание child само по себе не требует копирования всего workspace. Параллельная запись parent/child в пересекающиеся targets требует явной координации; завершение child не удаляет постоянную папку чата.

TerminalSessions дают независимый lifecycle; недоверенные команды child исполняются в обязательной Bubblewrap/egress границе своего чата, как и команды parent.

## Ожидание человека

Child MAY перейти в `input-required`, но запрос маршрутизируется через parent/task coordinator и A2A status. Child не обходит parent и не отправляет внешнее сообщение самостоятельно, если это не было явно делегированной capability.

## Cancellation и завершение

- Cancel parent рекурсивно запрашивает cancel children, кроме явно detached durable tasks с owner/orphan policy.
- Child agent получает scheduler cancellation signal и проверяет его на каждой
  safe boundary до нового model/tool dispatch; после сигнала новый вызов не
  начинается, а scheduler и child workflow сходятся в `canceled`, не `failed`.
- При budget exhaustion parent ждёт подтверждение отмены только bounded grace.
  Некооперативная Task остаётся durable и `cancel_requested`, её ID входит в
  partial result как `pending_tasks`; runtime не выдаёт ей ложный terminal state
  и не теряет её поздний result/notification.
- Task cancellation кооперативна до grace period, затем executor завершает process group и закрывает PTY.
- Parent MUST проверить terminal status до использования результата.
- Child failure не обязан завершать parent: модель получает structured failure и выбирает fallback.
- Parent не может объявить итог, зависящий от pending task, не отметив результат как незавершённый.

## Долгоживущие операции

### LONG-01. Отправка внешней задачи и ожидание

Согласованные имена инструментов:

| Инструмент | Поведение в целевой реализации | Изменение |
| --- | --- | --- |
| `core_agent_send_message` | Отправляет задачу доверенному внешнему агенту и возвращает идентификатор операции без ожидания её завершения | Доработка существующего инструмента |
| `core_task_wait` | Принимает идентификатор операции, сохраняет состояние и приостанавливает агента до результата или окончательного таймаута | Доработка существующего инструмента |
| `core_wait_until` | Приостанавливает агента до указанного времени; новое принятое сообщение будит раньше, после обработки агент может снова вызвать ожидание | Новый инструмент, поведение по LONG-03 |

- Отправка задачи внешнему агенту может вернуть идентификаторы и позволить
  нашему агенту продолжить независимую работу.
- Ожидание сохраняет состояние и освобождает worker, не держит активный model call.
- Во время ожидания не расходуется бюджет model turns/tool calls на polling.
- Родительская A2A Task остаётся нетерминальной, при ожидании внешнего агента —
  `working`.
- Ожидание переживает закрытие UI/stream и перезапуск сервиса.
- Удалённое ожидание чужого HITL не означает завершения задачи. Решение там
  принимает владелец удалённого агента; наш агент ожидает дальнейшего результата.
- Закрытие SSE stream само по себе не означает успех или провал удалённой задачи.
- Сохраняется соответствие локального handle удалённым `taskId` и `contextId`.
  Идентификаторы корневой задачи нельзя подставлять вместо remote IDs.

Polling через GetTask использует возраст операции от первого committed send
marker: первые 180 секунд — каждые 10 секунд, следующие 600 секунд
(до возраста 780 секунд) — каждые 30 секунд, затем — с закреплённым
`poll_interval_seconds` (по умолчанию 300 секунд). Если закреплённый интервал
меньше раннего интервала, используется меньший. Следующая проверка ограничена
ближайшей границей фазы и окончательным deadline. Временная ошибка GetTask
использует тот же график; Send/Cancel не получают повторов.
Возраст вычисляется по сохранённым `deadline - timeout_seconds` и текущему
server/DB clock, поэтому restart, повторный wait и смена worker не начинают
раннюю фазу заново. Изменение company settings не меняет закреплённый интервал.
Checkpoint/contract versions и набор полей сохраняются; миграция данных не нужна.
При обновлении сервиса уже сохранённый `next_poll_at` остаётся действующим,
адаптивный график применяется при следующем планировании проверки.
SSE/push могут ускорять обнаружение результата, если используются.
Расширяются существующие `core_agent_send_message`, `core_task_wait`
и task lifecycle вместо второго независимого набора сущностей для удалённых
операций. Имена закреплены; схемы аргументов и результатов определяются
при технической проработке API-контракта.

Исходящий adapter поддерживает объявленный Card binding JSONRPC либо HTTP+JSON
версии1.0. Send использует `returnImmediately: true`; Get/Cancel обращаются к
remote task ID, закодированному одним URL segment. Root task/context IDs и
входящий credential не передаются. Credential разрешается только из закреплённой
registry revision. URL интерфейса Card совпадает с зарегистрированными scheme,
authority и полным path, включая semicolon parameters; различие только завершающего
slash допустимо. Card не может переназначить authenticated request на другой target.
Discovery ограниченно повторяет temporary408/429/500/502/503/504; Send/Cancel
не повторяются adapter-ом. Parser сохраняет Task state, remote IDs и все Parts,
включая raw/URL files; malformed responses/IDs дают безопасную protocol error
без публикации содержимого ответа или parser diagnostics.

### LONG-02. Окончательный timeout операции

- Для ожидания задаётся предельный срок; его истечение окончательно закрывает
  возможность дальнейшего ожидания той же удалённой операции.
- Повторный wait немедленно возвращает, что эту операцию уже ожидали и срок истёк.
- В результате сообщается, что можно попробовать создать новую задачу.
- Не утверждается, что удалённая задача точно не завершилась: её исход может
  быть неизвестен, а побочный эффект уже выполнен.
- Deadline не продлевается повторным wait, follow-up или restart.
- По достижении deadline harness прекращает polling этой операции: новые
  GetTask и повторные подключения для получения результата не отправляются,
  запланированные проверки больше не исполняются, в том числе после restart.
  Перед каждым сетевым запросом проверяется актуальность ожидания и его срока.
- Если для ожидания используется SSE, локальная подписка закрывается и не
  восстанавливается. Дальнейший сбор результата и его публикация в чате
  с пометкой «получен поздно» не требуются.
- Ответ на запрос, отправленный до deadline, либо уже летящее SSE/push-событие
  всё же может прийти после timeout. Если timeout уже зафиксирован, такой
  результат игнорируется: он не меняет исход ожидания, не передаётся модели
  или в чат и не запускает продолжение заново.
- По timeout не отправляется автоматический CancelTask внешнему агенту.
- Новая удалённая задача создаётся отдельным действием; автоматическая повторная
  отправка после неоднозначного результата запрещена существующими гарантиями.

Пример результата: «Мы уже ожидали эту задачу, срок истёк. Повторное ожидание
недоступно; можно создать новую задачу. Исход предыдущей неизвестен».
Default — 24 часа с настройкой; это отдельный параметр от HITL.

Абсолютный remote deadline фиксируется до первого разрешённого dispatch Send;
ожидание локального HITL до этого не расходует remote timeout. Его настройки
и poll interval закрепляются для операции, последующие settings updates их
не меняют. Для remote handle `core_task_wait.timeout` отклоняется structured
`TOOL_ARGUMENT_INVALID` с пояснением использовать сохранённый deadline; optional
bounded timeout локальных task waits сохраняет прежнюю семантику.

Explicit cancel и owned-task cancellation фиксируют intent до внешнего вызова.
Если remote ID известен и операция не закрыта deadline, допустим один CancelTask;
неизвестный mutating outcome требует reconciliation, а не повторения. После
committed remote timeout даже последующий cancel не открывает network заново.

Remote operation использует существующий background task kind `remote_a2a` и
public snapshot `{task_id, state, result, error, revision}`. Timeout сохраняет
terminal `state: failed`, error code `REMOTE_OPERATION_TIMEOUT` и result ровно:
`{agent_name, reason: "timeout", message, remote_outcome: "unknown",
can_create_new_task: true}`. Message сообщает, что срок уже истёк, повторное
ожидание недоступно и можно создать отдельную новую задачу; он не утверждает
неизвестный remote outcome. Повторный wait/get/cancel возвращает тот же сохранённый
outcome без отправки запросов. Local task snapshots сохраняют прежнюю семантику.

Immutable contract version1 содержит ровно `version`, `tenant_id`, `owner_id`
(parent run ID), `peer_id`, `peer_revision`, `peer_name`, `url`, `binding`,
`message_id`, `task`, `timeout_seconds`, `poll_interval_seconds`; internal trace
parent MAY добавляться существующим scheduler. Mutable checkpoint version1
содержит ровно `version`, `send_started`, `deadline`, `remote_task_id`,
`remote_context_id`, `next_poll_at`, `cancel_started`. Начальный checkpoint имеет
оба marker=false и остальные nullable fields=null. Первый committed send marker
назначает deadline от server/DB clock плюс timeout contract; caller не задаёт
новый срок. Сохранённый deadline не меняется. Известные remote IDs не заменяются.
Секретный header отсутствует в contract/checkpoint/result.

Checkpoint updates используют existing task revision для compare-and-set и
текущую compute claim. Они увеличивают revision; terminal notification создаётся
только один раз вместе с terminal state/result/error и outbox. Между network
steps claim освобождается, Task остаётся working. Due/expiry проверяются повторно
под тем же scope/row lock; deadline выигрывает у позднего результата даже при
ещё действительной claim. Terminal result никогда не восстанавливает polling.
Generic cancel не подменяет неизвестный mutating outcome обычным canceled.
Shutdown/lease loss сохраняют recoverable operation, не изображают caller cancel.

После non-terminal ответа Task working snapshot сохраняет только безопасный
progress result `{agent_name, remote_state}`. `remote_state` принадлежит ровно
`TASK_STATE_SUBMITTED`, `TASK_STATE_WORKING`, `TASK_STATE_INPUT_REQUIRED` или
`TASK_STATE_AUTH_REQUIRED`. Это metadata, а не peer text или окончательный ответ.
Progress и checkpoint коммитятся одной fenced/CAS transaction; progress не
создаёт terminal notification, не разрешает wait и не расходует model/tool budget.
Terminal timeout/outcome заменяет working progress и выигрывает у позднего update.
Foreign human-wait остаётся working: согласование выполняет remote owner.

### LONG-03. Ожидание времени

Новый инструмент `core_wait_until` ожидает до заданного момента времени
с сохранением продолжения и восстановлением после restart.
Пробуждение позволяет проверить внешнее событие,
например доставку; само время пробуждения не доказывает, что событие произошло.

Новое принятое follow-up сообщение в эту задачу прерывает ожидание времени
и возобновляет агента сразу. Например, при ожидании до 18:00 сообщение в 15:00
обрабатывается в 15:00. Результат инструмента сообщает о пробуждении из-за
сообщения, а не о наступлении заданного времени; само сообщение поступает
перед следующим обращением к модели с обычными проверками guardrails.

Прежнее ожидание закрывается, его таймер больше не запускает продолжение.
После обработки уточнения агент сам решает, продолжать работу или снова вызвать
ожидание, в том числе до прежних 18:00. Новый вызов создаёт отдельное ожидание;
старый таймер и повторная доставка уже принятого messageId его не прерывают.
Пробуждение, сообщение и restart не должны приводить к двойному продолжению.
Правило относится только к ожиданию времени: HITL и ожидание внешней задачи
обрабатывают уточнения после завершения своего ожидания по TASK-03.

### LONG-04. Надёжность продолжения

Runtime MUST обеспечивать: устойчивый workflow checkpoint, абсолютные
deadline, сохранённый повод ожидания и атомарный выбор исхода при гонке события
с timeout. Повторное уведомление не запускает продолжение дважды. Локальная
дедупликация не означает exactly-once гарантию внешнего побочного эффекта.

## Расписания cron

### CRON-01. Создание и выполнение

- Владелец создаёт расписания в UI.
- Агент может создавать их отдельным инструментом с общей политикой tools.
- Этот инструмент можно запретить и убрать из каталога модели; управление через
  UI при этом остаётся доступным владельцам.
- Новый инструмент по умолчанию требует HITL.
- Каждый запуск создаёт новую полноценную задачу с сохранённым prompt.
- Это не непосредственный запуск произвольной shell-команды по расписанию.
- Все запуски одного расписания используют один чат и его папку.

### CRON-02. Перекрытие запусков

Если предыдущий запуск ещё активен, включая ожидание HITL или внешнего агента,
новый запуск пропускается. Он не ставится в очередь и не запускается параллельно.
В чате записывается факт и причина пропуска.

Это отдельное правило scheduler-а: пропущенный cron tick не обязан создавать
`failed` Task. `TASK-02` описывает ответ на входящую попытку создать новую задачу.
Ручной запуск описан в CRON-06.

### CRON-03. Пропуски во время недоступности сервиса

Если сервис был недоступен в запланированный момент, пропущенный запуск
не выполняется задним числом. После восстановления scheduler ждёт ближайшего
будущего момента по расписанию: не создаёт очередь пропущенных запусков
и не запускает одну компенсирующую задачу сразу после восстановления.
В чате расписания фиксируются пропуск и его причина.

Это правило относится к новым запускам, которые не были приняты до сбоя.
Уже принятая и сохранённая задача восстанавливается по общим правилам workflow;
restart не превращает её в пропущенный cron tick. Если она ещё активна
к следующему моменту расписания, применяется CRON-02.

### CRON-04. Отключение и удаление расписания

Отключение или удаление расписания прекращает только будущие запуски.
Уже принятая задача продолжает работу, включая ожидание HITL, ответа владельца
или внешнего агента, и сохраняет возможность восстановления после restart.
Для её остановки владелец отдельно отменяет саму задачу.

Чат, история, файлы и ожидающие решения не удаляются вместе с расписанием.
При гонке scheduler-а с отключением или удалением атомарно определяется,
был ли новый запуск уже принят: принятый продолжает работу, после вступления
отключения или удаления в силу новые задачи по расписанию не создаются.

### CRON-05. Часовой пояс расписания

Для каждого расписания владелец выбирает часовой пояс в UI. Значение
по умолчанию при создании — `Europe/Moscow`; тот же default применяется
при создании через инструмент агента, если пояс явно не указан.
Часовой пояс сохраняется вместе с расписанием как идентификатор IANA,
а не только фиксированное смещение UTC. Некорректное значение отклоняется
при сохранении без молчаливой подстановки другого пояса.

Расписание вычисляется в сохранённом часовом поясе. Перезапуск, часовой пояс
pod или браузера владельца не меняют моменты запусков. UI явно показывает
выбранный пояс и время ближайшего запуска в нём.

При переводе часов несуществующий local wall time пропускается. Повторяющийся
wall time выполняется один раз, в его более раннем occurrence. Следующий
момент всегда строго позже переданного UTC instant; второй occurrence того же
wall time не компенсирует уже прошедший первый. Календарный расчёт не зависит от
локального timezone процесса и возвращает timezone-aware UTC instant.

### CRON-06. Ручной запуск

В UI владельцу доступна кнопка «Запустить сейчас». Она создаёт новую задачу
с сохранённым prompt расписания в том же чате и workspace. Применяются обычные
правила контекста, доступа, инструментов, HITL и guardrails. Ручной запуск
не меняет cron-выражение, часовой пояс или время следующего запуска по расписанию.

Пока в чате есть активная задача, включая любое ожидание, кнопка недоступна.
Сервер проверяет занятость независимо от состояния UI и атомарно принимает
новую задачу. Если чат успел заняться, действует TASK-02: попытка завершается
как failed с причиной CONTEXT_BUSY, без выполнения и очереди.
Текущая задача продолжает работу. Если ручная задача ещё активна к очередному
cron tick, автоматический запуск пропускается по CRON-02.

### CRON-07. Изменение расписания

Изменения prompt, cron-выражения и часового пояса применяются только к будущим
запускам. При приёме задачи сохраняется согласованный снимок этих параметров;
уже принятая задача продолжает работу с исходным заданием, включая выход
из ожидания и восстановление после restart. Редактирование расписания
не подменяет её prompt и не добавляет ей follow-up.

Следующие автоматические и ручные запуски используют новые сохранённые
параметры. Само редактирование не запускает задачу и не выполняет пропущенные
запуски задним числом. При гонке редактирования с приёмом запуска задача
получает целиком одну версию параметров, без смешивания старых и новых полей.
Правила изменения политики инструментов из TOOL-01 и HITL-03/04 сохраняются:
снимок расписания не закрепляет устаревшие разрешения.

### CRON-08. Диалект выражений version1

Выражение содержит ровно пять полей: minute, hour, day of month, month,
day of week. Поддерживаются обычные числовые значения, `*`, списки через запятую,
неубывающие диапазоны и положительные целочисленные шаги. Для month/weekday
допустимы стандартные трёхбуквенные английские имена без учёта регистра;
Sunday обозначается 0 либо 7; имя `sun` нормализуется к 0, поэтому
`mon-sun` является обратным диапазоном (для понедельника–воскресенья используется
`mon-7`). Ограниченным считается поле, отличное от отдельного `*`, включая
явные полные диапазоны, шаги и списки. Day-of-month и day-of-week при одновременном
ограничении связаны OR; невозможный day-of-month сам по себе не отменяет
допустимые совпадения day-of-week.

Seconds/year fields, macros `@...`, `H/R/L/W/#/?`, обратные диапазоны,
некорректные поля и выражения без возможного следующего момента отклоняются
как `CRON_INVALID`, без файловых/DB effects и без текста исключения парсера.
Expression ограничен 256 UTF-8 байтами. Поиск следующего календарного совпадения
ограничен восемью годами; все public create/update paths проверяют следующий
момент до сохранения. Пробельное разделение нормализуется к одному пробелу.
Синтаксис и итерация используют закреплённый mature cron parser; собственный
minute-by-minute parser или цикл по всем пропущенным годам не вводятся.


### CRON-09. Admission, идемпотентность и доступ

Owner create может привязать расписание к существующему canonical чату компании
либо создать новый пустой owner chat без Task, вызова модели или workspace
mutation. Несколько расписаний могут использовать один чат; занятость определяет
последний canonical root этого чата, а не только предыдущая задача расписания.
Пустой чат имеет `latest_task_id: null`, пустую историю и тот же `context_id`
после первого запуска. Удаление расписания сохраняет такой чат.

Автоматический запуск использует сохранённую company/chat/owner binding через
внутренний server-owned origin, без loopback HTTP, JWT или owner authority.
Внешний агент и модель не могут сформировать такой origin или стать approver.
Ручной запуск сохраняет authenticated actor владельца отдельно от execution
owner чата. Любой root получает immutable `cron_origin` version1: schedule ID,
revision, source `automatic|manual`, due instant для automatic и согласованный
prompt/expression/timezone. Это provenance, а не источник новых разрешений.

Admission root, его creation receipt, event запуска и продвижение `next_due_at`
автоматического расписания сохраняются атомарно. Ручной запуск не продвигает
`next_due_at`. Автоматический busy tick фиксирует только пропуск. Pending cleanup
блокирует автоматический запуск с пропуском `workspace_cleanup_pending`; ручной
запрос получает обычную pre-admission retryable ошибку без receipt.

Create и run-now требуют client `request_id`: 1–256 UTF-8 bytes без NUL и
некорректного Unicode. Company-scoped receipt связывает ID с нормализованным
телом запроса; тот же запрос возвращает прежний результат, иное тело — conflict.
Retry ранее принятого run-now возвращает ту же Task даже после изменения,
отключения или удаления расписания. Новый run-now выключенного расписания
отклоняется до admission. Edit/delete/run-now используют `expected_revision`,
целое >=1 без boolean; stale revision даёт conflict. Tool create закрепляет
receipt за исходным run/call и fenced lease: late completion после потери lease
или cancel не создаёт расписание. Запрет tool не ограничивает owner CRUD.

### CRON-10. Coordinator и записи пропусков

Для здорового coordinator допускается lateness до 60 секунд включительно по
DB clock. Более поздний непринятый tick пропускается с reason `late`. При startup,
смене leader или перерыве более 60 секунд между завершёнными passes фиксируется
recovery cutoff; все ещё не принятые due instants <=cutoff пропускаются независимо
от grace с reason `service_unavailable`, следующий due строго позже cutoff.
Уже принятые roots восстанавливаются обычным workflow coordinator.

Для компании работает один coordinator, включая rolling update. PostgreSQL
session advisory leader lock и automatic occurrence transaction используют одну
connection; потеря session не позволяет другой connection коммитить под прежним
leadership. Unknown commit outcome разрешается durable receipt/event dedup,
без optimistic продвижения или blind retry. Один pass обрабатывает не более
100 candidates, с продолжением обычного recovery между passes. Ошибка отдельной
записи не должна навсегда блокировать обработку остальных расписаний.

Пропуск сохраняется append-only событием с reason `context_busy|late|
service_unavailable|workspace_cleanup_pending`, schedule revision, timezone и
моментом либо интервалом `{first_due_at, through}`. Интервал не обещает exact
count пропущенных ticks и не требует перебора всех минут downtime. UI показывает
событие в исходном чате, включая до первой Task. Оно имеет immutable history
anchor и stable cursor, не меняет terminal transcript, не становится user turn
для модели и не открывается внешнему агенту. Ordinary history entry IDs и
ранее выданные cursors остаются совместимыми.
