# Инструменты и approvals

## Единая модель tool

Встроенные и MCP-инструменты представляются одинаково:

```text
name, namespace, description, input_schema, output_contract, risk_metadata
```

Полное имя MUST быть namespaced, например `core.terminal.exec` или `github.create_issue`. Коллизия имён MUST завершать инициализацию с `TOOL_NAME_COLLISION`.

Arguments MUST валидироваться по schema до risk assessment и исполнения. Модель не может вызвать незарегистрированный tool.

## Встроенные capabilities

Ядро предоставляет базовый набор, но AgentConfig MAY отключить любой model-callable built-in или целую optional feature. Модель видит только EffectiveConfig catalog:

### `core.terminal.exec`

Запускает process в принадлежащей agent-у [TerminalSession](execution-environment.md) с явными `argv`, local workspace, environment allowlist и timeout. Main и каждый child имеют разные session/process group/workspace. Shell-строка MAY поддерживаться policy, но считается более рискованной, чем `argv`.

Возвращает `exit_code`, ограниченные `stdout`/`stderr`, duration и session identifier для продолжающегося процесса.

### `core.terminal.write`

Передаёт input существующему PTY/process или запрашивает его текущее состояние. Не может адресовать session другого agent/run.

### `core.fs.apply_patch`

Атомарно применяет текстовый patch внутри разрешённых workspace roots и возвращает список изменённых файлов. Patch, выходящий за root, отклоняется до изменения файлов.

### `core.input.request`

Создаёт typed human input request, когда задачу нельзя безопасно продолжить без новых данных. Это не approval и не даёт разрешение на side effect.

### `core.delegate`

Создаёт неблокирующую child-agent A2A Task с явными instruction, tool/MCP/skill allowlists, budget и result schema. Memory access существует только как явно переданный Memory MCP server/tools. Недоступен, если delegation отключён policy.

### Task tools

`core.task.start/get/list/wait/cancel` управляют background Tasks. `wait` является passive durable wait, не busy loop. Полная semantics описана в [Фоновых задачах и делегировании](tasks-and-delegation.md).

### Memory tools

Memory tools не являются built-ins Core Agent. Их предоставляет отдельный [Memory MCP Service](memory-service.md) под namespace вроде `memory.search`, `memory.read`, `memory.create`, `memory.update`, `memory.split`. AgentConfig может отключить memory полностью или отфильтровать отдельные tools.

### Artifact tools

Позволяют сохранить, прочитать релевантный range, проверить digest и передать ссылку на большой output без помещения всего содержимого в context.

Дополнительные native filesystem/search tools SHOULD появляться там, где они дают более строгую path validation и structured output, чем shell. Terminal остаётся универсальным fallback, а не способом обойти typed tool policy.

## Нормализация результата

Каждый tool result MUST содержать:

- `tool_call_id`;
- статус `succeeded`, `failed`, `denied` или `timed_out`;
- короткий model-facing result;
- ссылку на полный artifact, если output превышает лимит;
- duration и attempt;
- описание фактического side effect, если он был.

Обрезка output MUST быть явно помечена. Полный output сохраняется как artifact, если это разрешено data policy.

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
