# Инструменты

## Единая модель tool

Встроенные и MCP-инструменты представляются одинаково:

```text
name, namespace, description, input_schema, output_contract
```

Полное имя MUST быть namespaced и MUST состоять только из `[A-Za-z0-9_-]`, например `core_terminal_exec` или `github_create_issue`. Коллизия имён MUST завершать инициализацию с `TOOL_NAME_COLLISION`.

Точка в каноническом имени запрещена, потому что её не принимает ни один распространённый model API: имя пришлось бы переписывать на границе модели, и тогда в каталоге стояло бы одно имя, а в аргументах, allowlist-ах, audit и логах — другое. Модель, которую просят назвать tool внутри аргумента, называет его так, как увидела; расхождение делает такой вызов невыполнимым, а причину — невидимой. Имя MCP-тула образуется из имени сервера и имени тула, а обратное соответствие `(сервер, tool)` MUST храниться индексом: сервер вправе опубликовать tool, в имени которого уже есть точка, и разбор имени назвал бы не тот сервер.

Arguments MUST валидироваться по schema до исполнения. Модель не может вызвать незарегистрированный tool.

Model-facing description кратко задаёт назначение, критерий выбора и критичные ограничения конкретного tool. Общие safety/kernel правила не копируются в каждый description. Description не обещает ownership, isolation, idempotency или lifecycle, которых runtime фактически не обеспечивает.

Каноническое имя tool используется в runtime, transcript, audit, A2A, telemetry и в аргументах тех тулов, которые ссылаются на другие тулы. Оно же уходит модели без изменений. Alias остаётся только для двух случаев, которых каноническое имя само по себе не решает: фактическая коллизия и provider length limit; тогда adapter добавляет hash suffix и MUST отобразить имя обратно до выхода из model boundary. Alias не является product API, не раскрывается пользователю и не интерпретируется как версия, instance ID или security metadata.

## Встроенные capabilities

Ядро предоставляет базовый набор, но AgentConfig MAY отключить любой model-callable built-in или целую optional feature. Модель видит только EffectiveConfig catalog:

### Служебные инструменты skills

`core_skill_activate` и `core_skill_read_resource` реализуют progressive
disclosure пакетов навыков. Это условные model-callable tools, а не независимые
capabilities: они отсутствуют при пустом effective-каталоге skills, не входят в
operator built-in allowlist и не перечисляются отдельно в
`core_delegate.tools`.

`core_skill_activate` принимает непустой список имён, enum которого строится из
skills текущего EffectiveConfig. Он закрепляет `SKILL.md` выбранных пакетов и со
следующего model turn добавляет их полные инструкции в контекст. До активации
контекст содержит только имена и краткие descriptions всех доступных skills.
Выбор выполняется моделью по смыслу задачи; буквальное присутствие имени в prompt
или slash-команда не требуются.

`core_skill_read_resource` появляется только для ресурсов уже активированных
skills и принимает один enum-идентификатор `<skill>/<relative-path>`. Он читает
bounded UTF-8 текст через SkillResolver, не принимает filesystem paths и не
исполняет scripts. Результат содержит идентификатор, content и digest; package
root, абсолютные пути и внешние файлы модели не раскрываются.

Оба tool проходят обычные schema, capability, budget, audit и OTel gates. Каждая
попытка расходует общий `tool_calls` budget. Ошибка активации или чтения
возвращается как structured failed tool result и сама по себе не завершает Task.

### `core_terminal_exec`

Запускает process в принадлежащей agent-у [TerminalSession](execution-environment.md) с явными `argv`, chat workspace, environment allowlist и timeout. Main и каждый child имеют разные session/process group; постоянный workspace принадлежит их чату, отдельные scratch-копии допустимы для изоляции изменений. `argv` исполняется напрямую без implicit shell: metacharacters вроде `&&` не интерпретируются. Если нужен shell, модель MUST явно вызвать его, например `{"argv":["sh","-lc","command-a && command-b"]}`, а policy оценивает этот вызов как часть arguments.

Образ v1 MUST предоставлять GNU coreutils/findutils/gawk/sed/grep, `rg`, `fd`, `file`,
`tree`, `xxd`, `uchardet`, `jq`, Mike Farah `yq`, `xmlstarlet`, `sqlite3`,
`curl`, `bsdtar`, `zip`/`unzip`/`7z`, `zstd`/`xz`/`bzip2`,
`ip`/`ss`/`nc`, `openssl`, `pdftotext`/`pdfinfo`/`pdftoppm`/`pdfimages` и
`qpdf`. Model-facing description MUST
кратко называть этот набор и говорить, что он не является allowlist: модель MAY
использовать другие доступные команды и устанавливать дополнительные инструменты
в workspace, если это разрешают policy и сеть. Description MUST NOT обещать
системную установку пакетов или права `root`.

Возвращает `exit_code`, ограниченные `stdout`/`stderr`, duration и session identifier для продолжающегося процесса.

### `core_terminal_write`

Передаёт input существующему PTY/process или запрашивает его текущее состояние. Не может адресовать session другого agent/run.

### `core_python_exec`

Выполняет bounded Python-код отдельным process в принадлежащем run workspace и предоставляет синхронный proxy `tools.call(canonical_name, arguments)` плюс immutable `tools.names`. Вложенный вызов built-in или MCP tool MUST повторно пройти EffectiveConfig, schema validation, policy, общий tool-call budget, owner/tenant checks, audit и дочерний OTel span. `core_python_exec` не может вызывать самого себя и не запускается через `core_task_start`.

Agent SHOULD использовать Python для runtime-dependent, non-trivial или accuracy-sensitive deterministic computation, parsing/validation и небольшой synchronous композиции разрешённых tools. Тривиальная language work не требует process call. Текущее время MUST проверяться доступным authoritative runtime tool; при Python используются `datetime.now().astimezone()` и явный timezone/UTC offset, а указанная timezone конвертируется через `zoneinfo`, если доступна. Direct OS/process/network Python calls не подменяют отсутствующий agent tool и не проходят `tools.call` broker; direct OS/process/network calls ограничены обязательным Bubblewrap и egress profile.

Capability присутствует в обоих runtime-профилях и управляется только built-in allowlist.

Код, timeout, cwd и output limit валидируются до запуска. Process использует очищенный environment, тот же owned workspace/process-group lifecycle и те же обязательные ограничения Bubblewrap и сетевой границы, что terminal. В `without_terminal` этот внутренний process backend не публикует terminal tool, но Python может импортировать `os`/`subprocess`; поэтому режим не является security sandbox от локальных команд. Ненулевой exit, exception, timeout и truncation нормализуются как обычный model-facing tool result; raw credentials в Python process не передаются.

### `core_fs_apply_patch`

Атомарно применяет текстовый patch внутри разрешённых workspace roots и возвращает список изменённых файлов. Patch, выходящий за root, отклоняется до изменения файлов.

### `core_delegate`

Создаёт child-agent A2A Task с outcome-oriented instruction, minimum sufficient tool/MCP/skill allowlists, budget и optional result schema.

Schema этого tool MUST собираться под конкретный run: `tools` перечисляется enum-ом фактического каталога тулов родителя, `skills` — enum-ом его skills. Свободная строка на этом месте предлагает модели придумать идентификатор и переносит несовпадение на момент после вызова; enum сообщает ровно то же самое там, где модель уже читает, и остаётся проверяемым.

Schema MUST также описывать назначение `instruction`, `tools`, `skills`,
`budget.turns`, `budget.tool_calls` и `background`. Budget schema MUST требовать
оба поля одновременно с `minimum: 1`; пропущенное поле не получает implicit
default. Пустой enum означает, что capability этого типа передать нельзя; модель
не должна подставлять произвольное слово вместо отсутствующего значения.

Отдельного аргумента для MCP-тулов у этого tool нет: они перечисляются в том же `tools` под именами каталога, а runtime раскладывает список на built-ins и `(сервер, tool)` своим индексом. Runtime выдаёт ровно перечисленные capabilities, но child самостоятельно выбирает метод внутри objective/scope. По умолчанию tool пассивно ждёт terminal child result; `background: true` возвращает handle только для независимой работы. Memory access существует только как явно делегированные `core_memory_*` tools. Недоступен, если delegation отключён policy.

Description и conditional delegation prompt MUST формулировать balanced decision
rule: использовать tool для независимой параллельной работы с материальной
экономией времени, изоляции большого отделимого context либо bounded independently
verifiable deliverable. Они MUST запрещать делегирование простой/короткой,
строго последовательной, неясной, тесно связанной, дублирующей работы, обход
policy/approval и generic second opinion без самостоятельного deliverable.

### Task tools

`core_task_start/get/list/wait/cancel` управляют background Tasks. `start` не принимает task/delegate/Python tools. `wait` является passive durable wait, не busy loop, для локального bounded wait при timeout возвращает текущий snapshot; remote handle подчиняется окончательному deadline LONG-02, который повторный wait не продлевает. Полная semantics описана в [Фоновых задачах и делегировании](tasks-and-delegation.md).

### Memory tools

`core_memory_search/read/create/update/split/delete` дают модели долговременную память поверх сессий. Это built-ins Core Agent: отдельного memory-сервиса и MCP-роли `memory` не существует. Аргумент `scope` принимает только `user` или `session`; конкретный namespace, `user_id` и `session_id` подставляет runtime, поэтому модель не может обратиться к чужой памяти подбором аргумента. Модель передаёт заголовок и тело документа, front matter формирует runtime.

Ошибка memory tool возвращается модели как failed tool result, а не завершает run: протокол 200 строк требует, чтобы модель получила `MEMORY_FILE_TOO_LARGE` и ответила на него вызовом `core_memory_split`. Backend выбирает `MEMORY_STORAGE_TYPE` (`in-memory`, `postgres`); backend является интеграцией и не управляет доменной семантикой. AgentConfig может отключить память полностью через `CORE_AGENT_MEMORY=disabled` или отфильтровать отдельные tools.

Полный контракт scope, tool schemas, лимита 200 строк, backends, эмбеддингов и hybrid retrieval определён в [Памяти агента](memory-service.md).

### Файлы и A2A результаты

`core_artifact_save`, `core_artifact_load`, `core_artifact_list` удалены из model catalog, registry, schemas и handlers. Их прежние allowlist names отклоняются с диагностикой конфигурации, stale calls не выполняются. Файлы доступны через workspace чата и проверенный UI/A2A transport по [Файлам](artifacts.md).

Обычный model/child text result публикуется adapter-ом как стандартный A2A Task Artifact без дополнительного model turn или tool call.

### `core_response_files`

Выбирает весь набор файлов следующего финального ответа, не отправляя сообщение.
Схема — ровно `{paths: string[]}`: уникальные непустые относительные пути обычных
файлов текущего chat workspace. Absolute paths, URLs, traversal, symlinks,
hardlinks и special files запрещены. `[]` очищает выбор. Два успешных вызова
заменяют набор целиком, а ошибка сохраняет прежний набор и исходные файлы.
Tool доступен только при настроенном trusted chat workspace и immutable transport
storage; проходит обычные capability intersection, schema, policy/HITL, общий
budget, guardrails и audit, также через Python broker и child allowlist.

Весь набор безопасно читается и проверяется до сохранения нового выбора.
Возвращаются ordered receipts `{file_id,name,media_type,size_bytes,sha256}`.
`ATTACHMENTS_TOO_LARGE` содержит `allowed_bytes` и `actual_bytes`; остальные
recoverable ошибки — `FILE_NOT_FOUND`, `INVALID_FILE_PATH`, `FILE_CHANGED`,
`FILE_READ_FAILED`, `ARTIFACT_INTEGRITY_FAILED`. Receipt и versioned immutable
manifest фиксируются одним workflow transition. Child выбирает только свой
результат; parent не получает автоматический root attachment set.
Выдача и recovery описаны в [FILE-03](artifacts.md#file-03-выдача-и-отправка).

### `core_agent_send_message`

Optional `files` задаёт выбранные моделью относительные workspace paths только
для этого вызова. Отсутствие поля и `[]` означают отсутствие вложений; никакой
автоматической отправки файлов workspace или pending final selection нет.
Весь набор сохраняется до HITL, общий decoded limit проверяется до отправки;
ошибка сохраняет исходники и возвращается модели без создания remote handle.

Отправляет одну задачу доверенному внешнему агенту из реестра владельцев и возвращает локальный operation handle без ожидания завершения. `core_task_wait` durable ожидает этот handle; remote IDs, deadlines, прогресс и auth описаны в [Удалённых A2A-агентах](tasks-and-delegation.md#удалённые-a2a-агенты). Входящий credential не проксируется; server использует secret-header configuration конкретного адресата. Ответ удалённого агента недоверенный, вызов не запускается через `core_task_start`.

### `core_wait_until`

Сохраняет отдельное ожидание до абсолютного момента; новый принятый follow-up будит его раньше с причиной `message`. Старый timer/duplicate message не будит следующее ожидание. Runtime освобождает worker; полные правила LONG-03 определены в [Ожиданиях](tasks-and-delegation.md).

Единственный аргумент — обязательный `until` (ISO-8601 строка с явным UTC offset
или `Z`); неизвестные поля и naive datetime отклоняются как `TOOL_ARGUMENT_INVALID`.
Прошедший срок возвращает результат сразу. Результат содержит нормализованный
`until`, `woke_at` в UTC и `reason: time|message`; при сообщении также `message_id`.
Пробуждение не утверждает, что ожидаемое внешнее событие произошло.

### Создание cron

`core_cron_create` принимает ровно `{prompt, expression, timezone?}` и создаёт
расписание текущего canonical чата. Default timezone —`Europe/Moscow`; tenant,
owner, context, source и request identity модель не задаёт. Tool mutating с
risk `external_write`, доступен в обоих runtime modes только внутри capability
intersection и live tool policy. По умолчанию требуется HITL для exact subject; cron approval дополнительно
содержит `resolved_parameters.timezone` с сохранённым IANA значением либо default
`Europe/Moscow`. Это metadata для владельца и digest/stale проверки, а исходные
`arguments` сохраняются без подмены. Direct и nested Python calls используют
одну процедуру построения subject; старое решение с иным subject не dispatch-ится.
Python broker и delegation повторно применяют те же проверки. Deny удаляет tool
из model catalog и отвергает stale dispatch; owner UI сохраняет доступ.
Повтор accepted call использует deterministic receipt; создание fenced текущим
workflow lease и не повторяется после unknown outcome. Результат —metadata
сохранённого расписания. Семантика CRON-01–10 определена в
[Расписаниях](tasks-and-delegation.md).

Дополнительные native filesystem/search tools SHOULD появляться там, где они дают более строгую path validation и structured output, чем shell. Terminal остаётся универсальным fallback, а не способом обойти typed tool policy.

## Нормализация результата

Каждый tool result MUST содержать:

- `tool_call_id`;
- статус `succeeded`, `failed`, `denied` или `timed_out`;
- короткий model-facing result;
- duration и attempt;
- описание фактического side effect, если он был.

Обрезка output MUST быть явно помечена. Model-callable artifact storage не используется как скрытый канал для полного output.

Ограниченная диагностика MUST называть, что именно не прошло: путь аргумента и ожидание против фактически переданного. Код без сообщения не является диагностикой — `TOOL_ARGUMENT_INVALID` в ответ на `argv`, переданный строкой вместо массива, не сообщает модели ничего, и цикл «исправь аргументы», ради которого ошибка и возвращается модели, вырождается в перебор. Значения аргументов в сообщение не попадают: они являются содержимым запроса, а тип и путь — нет.

Детерминированная ошибка schema/contract validation до dispatch, ошибка запуска process, доказанно произошедшая до dispatch, и завершившийся outcome со статусом `failed` или `timed_out` MUST возвращаться модели как обычный tool result с безопасным стабильным error code и ограниченной диагностикой. Такой result сам по себе MUST NOT переводить родительскую A2A Task в `failed`: loop продолжается, чтобы модель могла исправить arguments, выбрать другой tool или объяснить проблему пользователю.

Исчерпанный tool-call budget является такой pre-dispatch ошибкой. Runtime
возвращает `BUDGET_EXCEEDED` для каждого неисполненного request в assistant
batch, не запускает handler и предписывает модели перейти к честному
промежуточному результату в зарезервированном финальном turn. Result MUST
называть `tool_calls`, used/limit и MUST NOT включать придуманный output.

Это правило не применяется, когда side effect мог начаться, но его outcome неизвестен. Любая такая неопределённость для mutating/MCP call MUST завершаться reconciliation либо `SIDE_EFFECT_UNKNOWN` и не может быть понижена до model-facing recoverable failure.

## Elicitation

MCP elicitation нормализуется в A2A `input-required`. MCP server не общается с пользователем напрямую и не выбирает UI. Запрос секретного значения MUST быть преобразован в secret-reference flow, а не обычное текстовое поле.

### MCP allowlist

`MCP_ALLOWED_TOOLS` перечисляет имена тулов, а не пары «сервер плюс тул». Голое имя разрешает тул на любом подключённом сервере; форма `server.tool` дополнительно ограничивает его одним сервером. Обе формы принимаются одновременно, потому что имя тула у MCP-сервера само может содержать точку, и требовать от оператора угадывать разбор нельзя.

`MCP_READ_ONLY_TOOLS` использует формы голого `tool` и `server.tool`, но является
отдельной доверенной политикой развёртывания для классификации побочных эффектов.
Здесь scoped-форма читается однозначно и только для названного подключённого
сервера: в отличие от совместимого allowlist она не дублируется как голое имя с
точкой на остальных серверах. Любой MCP-инструмент, которого нет в этом списке,
MUST считаться изменяющим независимо от его имени и заявленных сервером
annotations. Серверная metadata MAY использоваться только как дополнительный
сигнал для оператора и не может сама разрешить автоматический повтор вызова с
неизвестным outcome.

Компромисс зафиксирован: голое имя, совпавшее у двух серверов, разрешает тул у обоих. Оператору, которому нужна изоляция, следует писать `server.tool`.

Разрешение не создаёт тул: имя, отсутствующее в каталоге сервера, отбрасывается при пересечении с фактическим каталогом.

Подключённый сервер, у которого не разрешён ни один тул, MUST порождать наблюдаемое предупреждение с именем сервера. Такой сервер выглядит рабочим — соединение установлено, каталог получен, — но не даёт модели ничего; без предупреждения расхождение обнаруживается только по отсутствию ожидаемого поведения.

## MCP lifecycle

1. Провалидировать descriptor и policy.
2. Установить соединение, согласовать protocol version/capabilities и выполнить MCP initialize. Клиент MUST принимать все опубликованные ревизии MCP, с которыми он совместим, а не только самую новую: сервер выбирает версию из предложенной клиентом, и отказ от рабочей ревизии делает совместимый сервер недоступным без причины. Отказ по версии MUST называть и предложенную сервером, и принимаемые клиентом. Если сервер выдал session id заголовком `Mcp-Session-Id`, клиент MUST возвращать его в каждом последующем запросе к этому серверу: без него сервер вправе отклонить запрос, и tools/list не выполнится при успешном initialize.
4. Получить catalogs tools/resources/prompts и провалидировать schemas/metadata.
5. Добавить namespaced capabilities в snapshot и discovery index.
7. Закрыть соединение при терминальном состоянии.

Новый run MUST заново выполнить безопасные `initialize` и `tools/list`. Если
Streamable HTTP server масштабирован в ноль, transient transport failure при
этой фазе MUST запускать bounded exponential backoff до одного общего для всего
discovery данного run monotonic deadline `MCP_COLD_START_TIMEOUT_SECONDS`, по
умолчанию 300 секунд. Несколько последовательно проверяемых серверов не умножают
этот предел. Deadline включает сетевые попытки и паузы между ними; отдельная
попытка не может продлить его через `MCP_TIMEOUT` или
`MCP_SSE_READ_TIMEOUT`. До первой сетевой попытки workflow, исходные AgentConfig,
platform capability ceiling, MCP declarations и абсолютный срок ожидания MUST
быть сохранены durable, чтобы follow-up, cancel и recovery видели тот же Task и
не расширяли принятые capabilities после изменения deployment configuration.
Новая platform deny MAY дополнительно сузить этот ceiling. Перед первым
initialize каждого нового run и перед каждой повторной попыткой клиент MUST
удалить прежние session id и negotiated protocol version, потому что новый
экземпляр сервера не обязан знать состояние остановленного.
Новый session id и negotiated version принадлежат ровно одному run; параллельный
root/child/tenant run не может удалить, прочитать или заменить их.

Cold-start retry разрешён только для временной недоступности transport, включая
connection refusal/reset, преждевременное закрытие ответа, timeout, HTTP 408/429
и 5xx. Ошибка descriptor, TLS
verification, authentication/authorization, несовместимая protocol version,
невалидный JSON или schema/protocol error MUST завершить попытку сразу. Отмена
Task MUST прерывать ожидание. Повторы discovery не являются model turn или tool
call и не расходуют соответствующие budgets.

Дополнительные `MCP_HEADERS_JSON` не могут задавать или переопределять
управляемые transport-ом заголовки `Content-Type`, `Accept`, `Mcp-Method`,
`Mcp-Name`, `Mcp-Session-Id` и `MCP-Protocol-Version` независимо от регистра.
Такой конфликт MUST завершить startup ошибкой `CONFIG_INVALID`, а не нарушить
изоляцию session или согласование протокола.

После истечения deadline optional server исключается из effective catalog с
наблюдаемым `MCP_CONNECTION_FAILED`; run продолжается без его capabilities.
Required server возвращает тот же structured error. Уже отправленный
mutating `tools/call` не является cold-start discovery: при неизвестном outcome
он MUST перейти в reconciliation/`SIDE_EFFECT_UNKNOWN`, а не запускаться снова.

Успешно найденный catalog сохраняется в immutable snapshot. После потери
process-local connection восстановление создаёт отдельный durable cold-start
deadline и переподключается, но пересобирает EffectiveConfig из сохранённого
catalog, а не из временной доступности optional server. Поэтому временно
недоступный optional server не превращает checkpoint в `CHECKPOINT_INVALID` и
не меняет сохранённый capability ceiling. Каталог модели и dispatch используют
текущие доступность и schema только тех identities, которые входят в этот ceiling:
отсутствующий tool не публикуется модели, новая identity не добавляется. До
dispatch исчезнувшего tool возвращается `TOOL_UNAVAILABLE`; смена schema или
identity после HITL делает подтверждение устаревшим (`TOOL_APPROVAL_STALE`) и
не допускает исполнения. Неизвестный outcome уже отправленного mutating вызова
сохраняет правило `SIDE_EFFECT_UNKNOWN`. Ошибка required reconnect или несовместимый checkpoint
фиксирует терминальный outcome, а не оставляет Task в бесконечном `RUNNING`.

Ответ на запрос MUST выбираться по `id`, а не по порядку прибытия. Сервер вправе отправить в том же event stream `notifications/progress`, логи и собственные запросы до самого ответа, и медленный tool делает это почти всегда. Первый кадр потока — это, как правило, нотификация: у неё нет ни `result`, ни `error`, поэтому чтение «первого пакета» отдаёт модели пустой результат вместо данных, причём как успех. Такой отказ неотличим для модели от «сервер ничего не нашёл», и она отвечает пользователю выдуманным отсутствием данных. Кадры без `id` и кадры с чужим `id` MUST пропускаться до истечения `MCP_SSE_READ_TIMEOUT`.

`isError` в результате `tools/call` MUST превращаться в failed tool result. Это ошибка исполнения на стороне сервера, а не транспортная: протокол намеренно возвращает её кодом 200 с телом, и трактовка тела как успеха выдаёт модели текст ошибки под видом данных.

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

## Политика владельцев для каждого инструмента

### TOOL-01. Три режима для каждого инструмента

| Режим | Каталог модели | Вызов |
| --- | --- | --- |
| Разрешён | Инструмент виден | Выполнение после остальных проверок |
| Требует HITL | Инструмент виден | Каждый конкретный вызов ожидает решение владельца |
| Запрещён | Инструмент отсутствует | Сервер также отклоняет попытку вызова |

Политика применяется к built-ins и MCP. Новый инструмент по умолчанию требует
HITL. Верхние ограничения platform/tenant и текущего runtime mode сохраняются:
UI не разрешает возможность, запрещённую более строгим ограничением.
Рекламируемые built-ins в Agent Card также фильтруются текущим owner deny при
каждом authenticated чтении, по company вызывающего principal.

Все пути выполнения, включая `tools.call(...)` из Python и делегирование,
проходят одинаковую серверную проверку. Policy и каталог заново учитывают
изменения владельца для последующих вызовов уже активной задачи. Начатый вызов
означает уже переданный на исполнение вызов; он не прерывается из-за изменения
настройки. Ожидание HITL сохраняется при переходе в автоматический режим
по HITL-03 и сразу завершается при запрете инструмента по HITL-04.

### TOOL-02. Исключение доверенного инструмента из guardrails

Для каждого built-in и MCP tool владелец может через UI указать, что инструмент
доверенный и для него не нужна проверка guardrails. Это отдельная настройка от
трёх режимов TOOL-01: сама метка не разрешает запрещённый инструмент и не
заменяет решение HITL. Новый инструмент по умолчанию проверяется guardrails;
исключение включает только владелец.

Метка отключает guardrails и для аргументов вызова, и для результатов конкретного
инструмента: они не отправляются детектору и не создают связанных запросов
guardrails. Валидация аргументов по схеме, проверки прав, изоляция и лимиты
сохраняются в любом случае. Исключение не отключает отдельные проверки входящих
сообщений и файлов только потому, что они могут использоваться этим инструментом.
Ответ инструмента не становится системной инструкцией и не повышает права
агента из-за этой настройки.
Метка не отменяет уже зафиксированный отказ или timeout по конкретному материалу.

Модель, внешний caller и metadata самого MCP tool не могут установить эту метку.
Применение исключения связывается с идентичностью конкретного инструмента и
доверенной конфигурацией, а не с текстом его ответа. Исключение внешнего вызова
не отключает независимые проверки вложенных `tools.call(...)`.


### TOOL-03. Идентичность policy и точка передачи на исполнение

Policy хранится вне immutable EffectiveConfig, под ключом company, canonical
name и runtime-derived origin (builtin либо MCP server/tool). Новый origin не
наследует allow или guardrails exception прежнего инструмента с тем же alias.
Schema digest и arguments конкретного approval неизменяемы; владельцы выбирают
только allow/reject. Схема решения не содержит editable arguments.
Несовпадение текущей schema/origin с подтверждённым вызовом возвращает
`TOOL_APPROVAL_STALE` без dispatch; модель может создать новый вызов.

Проверка текущей policy revision, запись dispatch intent и передача владения
исполнителю образуют одну transactional admission boundary. Изменение policy,
закоммиченное до этой границы, влияет на вызов; уже переданный вызов не
прерывается. Owner approval сам по себе не является dispatch. Lock order:
policy row, затем run rows в стабильном порядке, затем wait rows. `deny` в той же
транзакции закрывает pending approvals этой identity во всех чатах company с
`POLICY_DENIED`; `allow` не меняет существующие ожидания и сроки. Последняя
проверка deny обязательна также для разрешённого, но ещё не переданного вызова.

### TOOL-04. Вопрос владельцу

`core_ask_owner` принимает ровно непустой `question` длиной до 16 384 символов.
Tool сначала проходит собственную tool policy, затем создаёт отдельный
owner_question с отдельным deadline. Ответ возвращается как `{"answer":"..."}`;
timeout — linked tool result `OWNER_ANSWER_TIMEOUT`. Отказ/timeout approval дают
`OWNER_APPROVAL_REJECTED`/`OWNER_APPROVAL_TIMEOUT`, позволяют модели продолжить
задачу и не создают owner question. Обычный A2A follow-up не является ответом.

### Служебный переход к публичному ответу

`core_response_begin` — условный model-only control tool основного запуска с закрытой пустой object schema, без side effects и пользовательских аргументов. Он отсутствует в operator capability allowlist, Agent Card и `core_delegate.tools`; его reserved canonical name не может быть заменён MCP tool. Модель вызывает его после завершения работы, необходимых approvals/guardrails и выбора response files, вместо генерации final draft в рабочем turn. Следующий обычный model turn имеет tools-free catalog и формирует предназначенный Task recipient публичный ответ. Вызов расходует обычный tool budget, следующий model call — обычный model budget. Прямой/nested dispatch и child run не меняют фазу. Семантика durable boundary и fallback определена в [runtime](runtime.md), transport preview — в [A2A](a2a-protocol.md).
