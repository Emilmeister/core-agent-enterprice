# Публичный контракт

Версия целевого контракта: `v1alpha1` до первой стабильной реализации.

Документ задаёт логическую модель. Конкретные HTTP, library и CLI adapters MUST сохранять её семантику.

## RunRequest

```json
{
  "prompt": "Исправь падающий тест и объясни причину",
  "mcp": [],
  "skills": []
}
```

Объект MUST содержать ровно три поля. Неизвестные поля MUST отклоняться с `INVALID_REQUEST`. Session, tenant, auth, trace и delivery semantics принадлежат transport envelope и не дублируются в body.

### `prompt`

- Непустая UTF-8 строка является обязательным shorthand.
- Целевой контракт также принимает массив content parts внутри того же поля: `text`, `artifact_ref`, `image_ref`, `audio_ref` и другие versioned modalities. Бинарные данные передаются artifact reference, не inline base64.
- Содержит один новый пользовательский turn. История stateful session подмешивается ядром, а не клиентом.
- Максимальный размер задаётся DeploymentConfig; превышение MUST завершаться до вызова модели.
- Ядро MUST сохранять исходный prompt дословно на всём протяжении запуска.
- Неподдерживаемая выбранным model route modality должна быть преобразована разрешённым adapter-ом либо отклонена до первого turn.

### `mcp`

Массив MCP connection descriptors. Пустой массив означает отсутствие внешних MCP-инструментов.

Целевой продукт MUST поддерживать stdio и Streamable HTTP; дополнительные MCP transports подключаются adapter-ами:

```json
{
  "name": "repo-tools",
  "transport": {
    "type": "stdio",
    "command": "repo-mcp",
    "args": ["--root", "."],
    "env_refs": {"TOKEN": "secret/repo-token"}
  }
}
```

```json
{
  "name": "issue-tracker",
  "transport": {
    "type": "streamable_http",
    "url": "https://mcp.example.test",
    "header_refs": {"Authorization": "secret/issues-auth"}
  }
}
```

Требования:

- `name` MUST быть уникальным в пределах запуска.
- Descriptor MAY включать запрошенные MCP capabilities: tools, resources, prompts и elicitation.
- Секреты MUST передаваться ссылками на host secret store, а не значениями.
- Descriptor MUST проходить host allowlist и approval policy до подключения.
- Сервер и список его tools фиксируются snapshot-ом запуска. Изменение списка MAY быть принято только после явного повторного discovery ядром.
- Descriptor содержит `required` (default `true`). Ошибка обязательного подключения завершает запуск с `MCP_CONNECTION_FAILED`; optional server отключается с наблюдаемым warning event.
- MCP roots, sampling и elicitation проходят те же policy и human-in-the-loop gates, что локальные аналоги.

Массив задаёт полный набор MCP capabilities, разрешённых конкретному run. Пустой массив не наследует MCP server из предыдущего run session.

### `skills`

Массив content-addressable ссылок на skill packages. Пустой массив означает работу только со встроенными возможностями.

```json
{
  "name": "release-notes",
  "source": "skill://company/release-notes@2.1.0",
  "integrity": "sha256-..."
}
```

Требования:

- `name` MUST быть уникальным в пределах запуска.
- `source` MUST использовать resolver, разрешённый host policy. Целевые schemes: `file`, `skill`, `oci` и `git+https`.
- `integrity` обязателен для immutable remote source; registry resolver MAY получить его из подписанного lock record.
- Пакет MUST пройти валидацию, описанную в [Skills](skills.md).
- Содержимое пакета и dependency graph MUST быть зафиксированы lock snapshot-ом на время запуска.
- Профиль поставки MAY поддерживать только подмножество source schemes.

Массив задаёт полный набор skills, разрешённых конкретному run. Session memory может помнить факт использования skill, но не активирует его без нового descriptor.

## Session transport

Логические операции transport-а:

- `session.create`, `session.get`, `session.close`, `session.export`, `session.delete`;
- `session.run(RunRequest)`;
- `run.get`, `run.events`, `run.control`.

SDK MAY представить session как object handle, HTTP — как resource path. Anonymous `run(RunRequest)` создаёт ephemeral session. Независимо от формы transport-а само ядро получает неизменённые три входа и отдельно проверенный execution context.

## Ответ запуска

Запуск возвращает упорядоченный поток `RunEvent`. Каждое событие содержит envelope:

```json
{
  "version": "v1alpha1",
  "run_id": "run_01...",
  "sequence": 12,
  "timestamp": "2026-07-10T19:30:00Z",
  "type": "tool.completed",
  "data": {}
}
```

- `run_id` генерируется ядром.
- `sequence` начинается с 1 и строго возрастает без повторов внутри запуска.
- Потребитель MUST иметь возможность восстановить порядок без доверия времени.
- Неизвестный `type` MUST игнорироваться клиентом ради forward compatibility.

Обязательные типы событий:

- `run.started`;
- `assistant.delta` и `assistant.message`;
- `tool.requested`, `tool.started`, `tool.completed`, `tool.failed`;
- `approval.required`, `approval.resolved`;
- `input.required`, `input.resolved`;
- `context.compacted`;
- `memory.updated`, `checkpoint.created`;
- `subrun.started`, `subrun.completed`, `subrun.failed`;
- `run.paused`, `run.resumed`, `run.recovering`;
- `run.completed`, `run.failed`, `run.cancelled`;
- `run.aborted`.

Полезные данные событий описаны в [Наблюдаемости](observability.md).

## Control-команды

Approval, дополнительный human input и lifecycle являются командами уже существующему запуску, а не полями RunRequest:

```json
{
  "type": "approval.resolve",
  "run_id": "run_01...",
  "approval_id": "apr_01...",
  "decision": "approve"
}
```

```json
{
  "type": "run.cancel",
  "run_id": "run_01..."
}
```

Допустимые решения: `approve`, `deny`. Approval MAY создать ограниченный grant для повторяющегося scope, если policy и UI явно это поддерживают; default — одно точное действие. Повторная или устаревшая команда возвращает `APPROVAL_ALREADY_RESOLVED`.

Другие control types: `input.resolve`, `run.pause`, `run.resume`, `run.cancel`. Control command содержит idempotency key и ожидаемую run revision для защиты от гонок.

## Терминальные состояния

Ровно одно из событий `run.completed`, `run.failed`, `run.cancelled`, `run.aborted` MUST быть последним событием запуска. После него ядро MUST NOT испускать новые события этого запуска.

`run.aborted` используется только когда ядро не может доказать безопасное продолжение, например при неопределённом side effect после потери worker-а. Обычная ошибка реализации или provider-а должна быть `run.failed`.

`run.completed` содержит:

```json
{
  "message": "Итоговый ответ пользователю",
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "tool_calls": 0,
    "compactions": 0
  }
}
```

## Совместимость

- Добавление нового event type является обратно совместимым.
- Добавление обязательного поля или изменение семантики существующего поля требует новой версии контракта.
- Экспериментальные поля MUST находиться в `data.experimental` и не могут быть обязательными для корректного клиента.
