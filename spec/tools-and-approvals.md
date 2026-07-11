# Инструменты и approvals

## Единая модель tool

Встроенные и MCP-инструменты представляются одинаково:

```text
name, namespace, description, input_schema, output_contract, risk_metadata
```

Полное имя MUST быть namespaced, например `core.terminal.exec` или `github.create_issue`. Коллизия имён MUST завершать инициализацию с `TOOL_NAME_COLLISION`.

Arguments MUST валидироваться по schema до risk assessment и исполнения. Модель не может вызвать незарегистрированный tool.

## Встроенные capabilities

Ядро MUST предоставлять базовый набор; deployment MAY отключать его части policy:

### `core.terminal.exec`

Запускает процесс в отдельном [ExecutionEnvironment](execution-environment.md) с явными `argv`, рабочей директорией, environment allowlist и timeout. Shell-строка MAY поддерживаться adapter-ом, но должна считаться более рискованной, чем `argv`.

Возвращает `exit_code`, ограниченные `stdout`/`stderr`, duration и session identifier для продолжающегося процесса.

### `core.terminal.write`

Передаёт input существующему процессу или запрашивает его текущее состояние. Не может адресовать процесс другого запуска.

### `core.fs.apply_patch`

Атомарно применяет текстовый patch внутри разрешённых workspace roots и возвращает список изменённых файлов. Patch, выходящий за root, отклоняется до изменения файлов.

### `core.input.request`

Создаёт typed human input request, когда задачу нельзя безопасно продолжить без новых данных. Это не approval и не даёт разрешение на side effect.

### `core.delegate`

Создаёт неблокирующую child-agent A2A Task с явными instruction, tool/MCP/skill allowlists, memory access, budget и result schema. Недоступен, если delegation отключён policy.

### Task tools

`core.task.start/get/list/wait/cancel` управляют background Tasks. `wait` является passive durable wait, не busy loop. Полная semantics описана в [Фоновых задачах и делегировании](tasks-and-delegation.md).

### Memory tools

`core.memory.search/read/create/update/split/move/delete/history/index_status/entity_resolve` являются единственным способом изменять Markdown memory и её graph/index revision. Правила выбора create/update и лимит 200 строк определены в [Sessions и memory](sessions-and-memory.md).

### Artifact tools

Позволяют сохранить, прочитать релевантный range, проверить digest и передать ссылку на большой output без помещения всего содержимого в context.

Дополнительные native filesystem/search tools SHOULD появляться там, где они дают более строгий sandbox и structured output, чем shell. Terminal остаётся универсальным fallback, а не способом обойти typed tool policy.

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

DeploymentConfig задаёт один режим:

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
- расширения sandbox или отключения защитного механизма.

Обычное чтение внутри workspace, поиск, получение metadata и применение обратимого patch внутри workspace MAY проходить без approval, если host policy не строже.

Явная просьба в prompt выполнить действие учитывается как intent, но MUST NOT автоматически отменять требования host policy.

## Approval request

Перед ожиданием ядро переводит A2A Task в `input-required` с `reason: approval_required` и прикладывает Message с нормализованным payload:

```json
{
  "type": "urn:core-agent:approval-request:v1",
  "data": {
    "approval_id": "apr_01...",
    "tool_call_id": "call_01...",
    "tool": "github.create_issue",
    "summary": "Создать публичный issue в org/repo",
    "arguments": {},
    "risks": ["external_write", "acts_as_user"],
    "scope_options": ["single_call", "session:github.create_issue:org/repo"]
  }
}
```

Требования:

- summary объясняет эффект понятным языком;
- arguments редактированы от секретов, но достаточно точны для решения;
- default scope всегда `single_call`;
- более широкий grant MUST быть выбран человеком явно и ограничен tool, action, resource, session, expiry и argument constraints;
- approval связан с digest нормализованных arguments;
- любое изменение arguments аннулирует approval и требует нового;
- до `approve` tool MUST NOT начинать side effect;
- `deny` возвращается модели как tool result;
- отмена запуска автоматически отклоняет все ожидающие approvals.

Approval timeout задаётся хостом. По истечении времени решение трактуется как `deny`, audit получает `approval.resolved`, A2A Task обновляет status, затем agent loop получает отказ.

## Grants и отзыв разрешения

Одобренный reusable grant хранит issuer, policy version, normalized scope, issued/expiry timestamps и audit provenance. Перед каждым использованием policy engine повторно проверяет identity, session, tool, action, resource, argument constraints, expiry, usage count и revocation.

Пользователь или оператор может отозвать grant. Отзыв влияет на новые calls и не отменяет уже завершённый side effect.

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
3. после grace period завершить process tree в ExecutionEnvironment;
4. закрыть MCP connections;
5. перевести A2A Task в `canceled` с описанием возможных незавершённых side effects.
