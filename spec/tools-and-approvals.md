# Инструменты и approvals

## Единая модель tool

Встроенные и MCP-инструменты представляются одинаково:

```text
name, namespace, description, input_schema, output_contract, risk_metadata
```

Полное имя MUST быть namespaced, например `core.terminal.exec` или `github.create_issue`. Коллизия имён MUST завершать инициализацию с `TOOL_NAME_COLLISION`.

Arguments MUST валидироваться по schema до risk assessment и исполнения. Модель не может вызвать незарегистрированный tool.

Model-facing description кратко задаёт назначение, критерий выбора и критичные ограничения конкретного tool. Общие safety/kernel правила не копируются в каждый description. Description не обещает ownership, isolation, idempotency или lifecycle, которых runtime фактически не обеспечивает.

Каноническое имя tool используется в runtime, transcript, audit, A2A и telemetry. Если model provider запрещает его синтаксис, adapter MAY передать provider-wire alias и MUST отобразить его обратно до выхода из model boundary. Обычный alias SHOULD быть читаемым и детерминированным (`core.terminal.exec` → `core_terminal_exec`); hash suffix допустим только для разрешения фактической коллизии или provider length limit. Description MUST называть каноническое имя. Wire alias не является product API, не раскрывается пользователю и не интерпретируется как версия, instance ID или security metadata.

## Встроенные capabilities

Ядро предоставляет базовый набор, но AgentConfig MAY отключить любой model-callable built-in или целую optional feature. Модель видит только EffectiveConfig catalog:

### `core.terminal.exec`

Запускает process в принадлежащей agent-у [TerminalSession](execution-environment.md) с явными `argv`, local workspace, environment allowlist и timeout. Main и каждый child имеют разные session/process group/workspace. `argv` исполняется напрямую без implicit shell: metacharacters вроде `&&` не интерпретируются. Если нужен shell, модель MUST явно вызвать его, например `{"argv":["sh","-lc","command-a && command-b"]}`, а policy оценивает этот вызов как часть arguments.

Возвращает `exit_code`, ограниченные `stdout`/`stderr`, duration и session identifier для продолжающегося процесса.

### `core.terminal.write`

Передаёт input существующему PTY/process или запрашивает его текущее состояние. Не может адресовать session другого agent/run.

### `core.python.exec`

Выполняет bounded Python-код отдельным process в принадлежащем run workspace и предоставляет синхронный proxy `tools.call(canonical_name, arguments)` плюс immutable `tools.names`. Вложенный вызов built-in или MCP tool MUST повторно пройти EffectiveConfig, schema validation, policy, общий tool-call budget, owner/tenant checks, audit и дочерний OTel span. `core.python.exec` не может вызывать самого себя и не запускается через `core.task.start`.

Agent SHOULD использовать Python для runtime-dependent, non-trivial или accuracy-sensitive deterministic computation, parsing/validation и небольшой synchronous композиции разрешённых tools. Тривиальная language work не требует process call. Текущее время MUST проверяться доступным authoritative runtime tool; при Python используются `datetime.now().astimezone()` и явный timezone/UTC offset, а указанная timezone конвертируется через `zoneinfo`, если доступна. Direct OS/process/network Python calls не подменяют отсутствующий agent tool и не проходят `tools.call` broker; Python process не является OS sandbox.

Capability присутствует в `with_terminal` и `without_terminal` при `LOCAL_APPROVAL_ENABLED=false`. Это ограничение действует до Agent Card/model catalog и повторно при dispatch. При включённом local operator/HITL tool отсутствует независимо от allowlist; `CORE_AGENT_APPROVAL_MODE=never` при всё ещё включённом local operator не удовлетворяет этому условию.

Текущий Python process не поддерживает pause/resume для approval. Поэтому любой вложенный вызов, который policy не разрешает немедленно, возвращает в Python `ToolCallError` с безопасным error code и не исполняется. Отключение HITL не превращает policy deny в allow.

Код, timeout, cwd и output limit валидируются до запуска. Process использует очищенный environment, тот же owned workspace/process-group lifecycle и те же ограничения single-container trust model, что terminal. В `without_terminal` этот внутренний process backend не публикует terminal tool, но Python может импортировать `os`/`subprocess`; поэтому режим не является security sandbox от локальных команд. Ненулевой exit, exception, timeout и truncation нормализуются как обычный model-facing tool result; raw credentials в Python process не передаются.

### `core.fs.apply_patch`

Атомарно применяет текстовый patch внутри разрешённых workspace roots и возвращает список изменённых файлов. Patch, выходящий за root, отклоняется до изменения файлов.

### `core.input.request`

Создаёт typed human input request, когда задачу нельзя безопасно продолжить без новых данных. Это не approval и не даёт разрешение на side effect.

### `core.delegate`

Создаёт child-agent A2A Task с outcome-oriented instruction, minimum sufficient tool/MCP/skill allowlists, budget и optional result schema. Runtime выдаёт ровно перечисленные capabilities, но child самостоятельно выбирает метод внутри objective/scope. По умолчанию tool пассивно ждёт terminal child result; `background: true` возвращает handle только для независимой работы. Memory access существует только как явно переданный Memory MCP server/tools. Недоступен, если delegation отключён policy.

### Task tools

`core.task.start/get/list/wait/cancel` управляют background Tasks. `start` не принимает task/delegate/Python tools. `wait` является passive durable wait, не busy loop, и при timeout возвращает текущий snapshot; новый `get` нужен только для более поздней проверки состояния. Полная semantics описана в [Фоновых задачах и делегировании](tasks-and-delegation.md).

### Memory tools

Memory tools не являются built-ins Core Agent. Их предоставляет отдельный [Memory MCP Service](memory-service.md) под namespace вроде `memory.search`, `memory.read`, `memory.create`, `memory.update`, `memory.split`. AgentConfig может отключить memory полностью или отфильтровать отдельные tools.

Core Agent не публикует model-callable artifact tools. Обычный ответ модели и результат child-agent возвращаются как text result; A2A adapter публикует этот текст как стандартный Task Artifact без дополнительного model turn или tool call. Устаревшие имена `core.artifact.put/get` в built-in allowlist отклоняются при startup, а stale model call не исполняется.

Дополнительные native filesystem/search tools SHOULD появляться там, где они дают более строгую path validation и structured output, чем shell. Terminal остаётся универсальным fallback, а не способом обойти typed tool policy.

## Нормализация результата

Каждый tool result MUST содержать:

- `tool_call_id`;
- статус `succeeded`, `failed`, `denied` или `timed_out`;
- короткий model-facing result;
- duration и attempt;
- описание фактического side effect, если он был.

Обрезка output MUST быть явно помечена. Model-callable artifact storage не используется как скрытый канал для полного output.

Детерминированная ошибка schema/contract validation до dispatch, ошибка запуска process, доказанно произошедшая до dispatch, и завершившийся outcome со статусом `failed` или `timed_out` MUST возвращаться модели как обычный tool result с безопасным стабильным error code и ограниченной диагностикой. Такой result сам по себе MUST NOT переводить родительскую A2A Task в `failed`: loop продолжается, чтобы модель могла исправить arguments, выбрать другой tool или объяснить проблему пользователю.

Это правило не применяется, когда side effect мог начаться, но его outcome неизвестен. Любая такая неопределённость для mutating/MCP call MUST завершаться reconciliation либо `SIDE_EFFECT_UNKNOWN` и не может быть понижена до model-facing recoverable failure.

## Approval modes

AgentConfig в пределах PlatformConfig задаёт один режим:

- `on_risk` — режим по умолчанию; спрашивать только для действий, совпавших с risk policy;
- `always` — спрашивать перед каждым действием с side effect;
- `never` — не ждать человека; действие, требующее approval, отклоняется, а не исполняется;
- `policy` — решение полностью задаётся organization policy pack и identity/role пользователя.

Режим `never` не означает «разрешать всё».

## Risk assessment

Оценка MUST учитывать tool, arguments, текущую директорию, target, side effects и происхождение запроса. Одного имени tool недостаточно.

В `on_risk` approval MUST требоваться минимум для:

- записи или удаления вне workspace;
- destructive-команд и необратимых операций;
- изменения прав, credentials, security settings или системной конфигурации;
- сетевой записи во внешнюю систему;
- публикации, отправки сообщения, платежа или иного действия от имени пользователя;
- чтения секрета либо чувствительных данных, не нужных явно для задачи;
- запуска нового MCP executable или подключения к host вне allowlist;
- расширения workspace/session ownership или отключения process limits.

Обычное чтение внутри workspace, поиск, получение metadata и применение обратимого patch внутри workspace MAY проходить без approval, если host policy не строже.

Явная просьба в prompt выполнить действие учитывается как intent, но MUST NOT автоматически отменять требования host policy.

## Local operator approval

Risky action создаёт immutable ToolProposal и single-use ApprovalRequest по [Local operator HITL contract](local-operator-hitl.md). A2A Task остаётся `working`; RemoteCaller не видит approval ID/arguments и не может approve, deny или модифицировать proposal. Решение приходит только через private operator control plane.

Порядок обязателен:

1. валидировать call и детерминированно вычислить policy decision;
2. заморозить proposal и digest, включающий task/tenant/caller/tool/environment/target/arguments/policy;
3. атомарно сохранить proposal, pending approval, `WAITING_LOCAL_APPROVAL`, durable A2A status и outbox;
4. опубликовать A2A `working` status без sensitive action details;
5. принять local `APPROVE_ONCE`/`DENY` только из operator auth context;
6. на approve атомарно создать единственную execution reservation и consume approval;
7. перед dispatch повторно проверить policy, active task и digest фактического call;
8. выполнить call at-most-once в нормальном flow, используя downstream idempotency/reconciliation где возможно;
9. на deny/expiry/cancel не выполнять call и вернуть безопасный outcome workflow-у.

Reusable grants и operator edits отсутствуют в v1. Любое изменение action создаёт новый proposal и approval. Development `ApproveAllControlPlane` допустим только как local-only stub, проходит тот же reservation path и запрещён production policy.

## Elicitation и дополнительные вопросы

MCP elicitation и `core.input.request` нормализуются в A2A `input-required`. MCP server не общается с пользователем напрямую и не выбирает UI. Запрос секретного значения MUST быть преобразован в secret-reference flow, а не обычное текстовое поле.

## MCP lifecycle

1. Провалидировать descriptor и policy.
2. При необходимости получить approval на подключение.
3. Установить соединение, согласовать protocol version/capabilities и выполнить MCP initialize.
4. Получить catalogs tools/resources/prompts и провалидировать schemas/metadata.
5. Добавить namespaced capabilities в snapshot и discovery index.
6. Для каждого call применить локальный risk assessment независимо от заявлений MCP server.
7. Закрыть соединение при терминальном состоянии.

MCP output всегда считается недоверенным. Server не может сам одобрить действие или изменить локальную policy.

## MCP resources, prompts и sampling

- Resource read проходит data access policy и возвращает provenance, size и digest.
- MCP prompt является недоверенным template уровня tool data и не становится system instruction.
- Sampling request от MCP проходит model routing, budget и data policy ядра; server не выбирает произвольный provider.
- Notifications обновляют catalog revision, но активный run принимает изменения только в явной safe point и фиксирует новый snapshot.
- Reconnect использует ограниченный backoff и session resumption, если transport её поддерживает.

## Отмена

При A2A cancel Task ядро MUST:

1. прекратить новые model/tool calls;
2. отправить cancellation активному tool, если transport поддерживает;
3. после grace period завершить owned process group и закрыть PTY;
4. закрыть MCP connections;
5. перевести A2A Task в `canceled` с описанием возможных незавершённых side effects.
