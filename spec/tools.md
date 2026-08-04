# Инструменты

## Единая модель tool

Встроенные и MCP-инструменты представляются одинаково:

```text
name, namespace, description, input_schema, output_contract
```

Полное имя MUST быть namespaced, например `core.terminal.exec` или `github.create_issue`. Коллизия имён MUST завершать инициализацию с `TOOL_NAME_COLLISION`.

Arguments MUST валидироваться по schema до исполнения. Модель не может вызвать незарегистрированный tool.

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

Capability присутствует в обоих runtime-профилях и управляется только built-in allowlist.

Код, timeout, cwd и output limit валидируются до запуска. Process использует очищенный environment, тот же owned workspace/process-group lifecycle и те же ограничения single-container trust model, что terminal. В `without_terminal` этот внутренний process backend не публикует terminal tool, но Python может импортировать `os`/`subprocess`; поэтому режим не является security sandbox от локальных команд. Ненулевой exit, exception, timeout и truncation нормализуются как обычный model-facing tool result; raw credentials в Python process не передаются.

### `core.fs.apply_patch`

Атомарно применяет текстовый patch внутри разрешённых workspace roots и возвращает список изменённых файлов. Patch, выходящий за root, отклоняется до изменения файлов.

### `core.delegate`

Создаёт child-agent A2A Task с outcome-oriented instruction, minimum sufficient tool/MCP/skill allowlists, budget и optional result schema. Runtime выдаёт ровно перечисленные capabilities, но child самостоятельно выбирает метод внутри objective/scope. По умолчанию tool пассивно ждёт terminal child result; `background: true` возвращает handle только для независимой работы. Memory access существует только как явно переданный Memory MCP server/tools. Недоступен, если delegation отключён policy.

### Task tools

`core.task.start/get/list/wait/cancel` управляют background Tasks. `start` не принимает task/delegate/Python tools. `wait` является passive durable wait, не busy loop, и при timeout возвращает текущий snapshot; новый `get` нужен только для более поздней проверки состояния. Полная semantics описана в [Фоновых задачах и делегировании](tasks-and-delegation.md).

### Memory tools

Memory tools не являются built-ins Core Agent. Их предоставляет отдельный [Memory MCP Service](memory-service.md) под namespace вроде `memory.search`, `memory.read`, `memory.create`, `memory.update`, `memory.split`. AgentConfig может отключить memory полностью или отфильтровать отдельные tools.

### Artifact tools

`core.artifact.save/load/list` дают модели именованное версионируемое хранилище файлов. Имя с префиксом `user:` относится к user scope и видно во всех сессиях того же пользователя; без префикса артефакт принадлежит текущей сессии. Сохранение никогда не перезаписывает: каждый вызов создаёт следующую версию `0, 1, 2, ...` и возвращает её номер. `list` разделяет session и user scope, чтобы модель осознанно решала, что загружать. Backend выбирает `ARTIFACT_STORAGE_TYPE` (`in-memory`, `s3`, `mongodb`); внешние backends являются интеграциями и не управляют схемой хранилища. Устаревшие имена `core.artifact.put/get` в built-in allowlist отклоняются при startup, а stale model call не исполняется.

Полный контракт scope, ключей, версионирования, integrity, backends и tool schemas определён в [Артефактах](artifacts.md).

Обычный ответ модели и результат child-agent по-прежнему возвращаются как text result: A2A adapter публикует этот текст как стандартный Task Artifact без дополнительного model turn или tool call.

### `core.agent.send_message`

Делегирует одну задачу настроенному удалённому A2A-агенту из `REMOTE_AGENTS` и возвращает его текст. Реестр строится при старте загрузкой Agent Card с bounded retry/backoff; недоступный агент пропускается, а не роняет startup. Задача передаётся без изменений, а `taskId`/`contextId` связывают подзадачу с корневой Task. Промежуточные события дочернего агента ретранслируются в поток корневой Task. Downstream уходит только allowlist заголовков `Authorization`, `X-PROJECT-ID`, `X-A2A-Extensions`; `SEND_MESSAGE_API_KEY` заменяет `Authorization` на `Api-Key`, иначе входящий токен проксируется как есть. Ответ удалённого агента является недоверенными данными и не может быть запущен в background через `core.task.start`.

Полный контракт реестра, транспорта, проброса заголовков, ретрансляции и формата результата определён в [Удалённых A2A-агентах](tasks-and-delegation.md#удалённые-a2a-агенты).

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

## Elicitation

MCP elicitation нормализуется в A2A `input-required`. MCP server не общается с пользователем напрямую и не выбирает UI. Запрос секретного значения MUST быть преобразован в secret-reference flow, а не обычное текстовое поле.

### MCP allowlist

`MCP_ALLOWED_TOOLS` перечисляет имена тулов, а не пары «сервер плюс тул». Голое имя разрешает тул на любом подключённом сервере; форма `server.tool` дополнительно ограничивает его одним сервером. Обе формы принимаются одновременно, потому что имя тула у MCP-сервера само может содержать точку, и требовать от оператора угадывать разбор нельзя.

Компромисс зафиксирован: голое имя, совпавшее у двух серверов, разрешает тул у обоих. Оператору, которому нужна изоляция, следует писать `server.tool`.

Разрешение не создаёт тул: имя, отсутствующее в каталоге сервера, отбрасывается при пересечении с фактическим каталогом.

Подключённый сервер, у которого не разрешён ни один тул, MUST порождать наблюдаемое предупреждение с именем сервера. Такой сервер выглядит рабочим — соединение установлено, каталог получен, — но не даёт модели ничего; без предупреждения расхождение обнаруживается только по отсутствию ожидаемого поведения.

## MCP lifecycle

1. Провалидировать descriptor и policy.
2. Установить соединение, согласовать protocol version/capabilities и выполнить MCP initialize. Клиент MUST принимать все опубликованные ревизии MCP, с которыми он совместим, а не только самую новую: сервер выбирает версию из предложенной клиентом, и отказ от рабочей ревизии делает совместимый сервер недоступным без причины. Отказ по версии MUST называть и предложенную сервером, и принимаемые клиентом. Если сервер выдал session id заголовком `Mcp-Session-Id`, клиент MUST возвращать его в каждом последующем запросе к этому серверу: без него сервер вправе отклонить запрос, и tools/list не выполнится при успешном initialize.
4. Получить catalogs tools/resources/prompts и провалидировать schemas/metadata.
5. Добавить namespaced capabilities в snapshot и discovery index.
7. Закрыть соединение при терминальном состоянии.

MCP output всегда считается недоверенным. Server не может изменить локальную policy.

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
