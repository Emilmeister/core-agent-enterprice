# A2A protocol

## Нормативная база

Основной внешний интерфейс Core Agent MUST соответствовать опубликованной [Agent2Agent Protocol specification](https://a2a-protocol.org/latest/specification/). Реализация фиксирует поддерживаемые A2A protocol versions и объявляет их через Agent Card/capability negotiation; ссылка `latest` используется документацией, но не runtime dependency resolution.

A2A является внешней моделью общения. MCP остаётся протоколом подключения tools/resources, а не transport-ом между клиентом и Core Agent.

## Agent Card

Core Agent публикует public Agent Card и, при наличии закрытых capabilities, authenticated extended Agent Card. Card MUST объявлять:

- A2A protocol version и bindings;
- streaming и push-notification capabilities;
- supported input/output media types;
- authentication schemes;
- public Agent Skills в терминах A2A;
- Core Agent extension URI и её required/optional status.

A2A Agent Skill описывает внешнюю capability сервера и не равен runtime skill package из RunRequest. Названия могут совпадать, но lifecycle и trust model различаются.

## Core Agent extension

Логические входы `prompt`, `mcp`, `skills` передаются через A2A:

- `prompt` — content A2A `Message.parts`;
- `mcp` и `skills` — versioned structured data extension `urn:core-agent:run-capabilities:v1` в A2A Message metadata, keyed by extension URI;
- session — A2A `contextId`;
- запуск/фоновая работа — A2A `Task`;
- результат — A2A `Artifact`;
- пользовательское объяснение или запрос данных — A2A `Message`.

Клиент MUST объявить поддержку required extension. Сервер MUST вернуть стандартную A2A ошибку для неподдерживаемой required extension, а не молча проигнорировать MCP/skills.

Клиент opt-in использует binding-specific A2A extension mechanism (`A2A-Extensions` service parameter для HTTP), перечисляет URI в `Message.extensions` и кладёт payload под тем же URI в `Message.metadata`.

Extension payload содержит ровно:

```json
{
  "mcp": [],
  "skills": []
}
```

`prompt` не дублируется в metadata. Auth, tenant, trace context, budgets и policy не становятся extension fields.

## Task mapping

| Core state | A2A Task state | Дополнительная семантика |
|---|---|---|
| `CREATED`, `QUEUED` | `submitted` | задача принята, worker ещё не выполняет turn |
| `RUNNING`, `WAITING_TASK`, `PAUSED`, `RECOVERING` | `working` | точная причина доступна в безопасном status metadata |
| `WAITING_INPUT`, `WAITING_APPROVAL` | `input-required` | `reason` различает input и approval |
| `WAITING_AUTH` | `auth-required` | требуется credential/auth flow |
| `COMPLETED` | `completed` | результаты представлены Artifacts |
| `FAILED`, `ABORTED` | `failed` | error metadata различает обычную ошибку и unsafe continuation |
| `CANCELLED` | `canceled` | отмена подтверждена runtime |
| `REJECTED` | `rejected` | policy отказала до выполнения |

Internal state не добавляет новые A2A terminal states. Client, понимающий только стандартный A2A, остаётся корректным.

## Операции

Core Agent MUST поддерживать A2A operations, необходимые для:

- send message: начать или продолжить task;
- send streaming message: получить status/artifact updates в реальном времени;
- get/list task: polling и управление несколькими фоновыми tasks;
- subscribe to task: восстановить stream активной task;
- cancel task;
- create/get/list/delete push notification configuration;
- получить extended Agent Card, если capability объявлена.

Конкретный binding MAY временно поддерживать подмножество optional operations только если Agent Card честно отражает capability.

## Blocking, background и ожидание

- Простое взаимодействие MAY вернуть direct A2A Message.
- Любая работа с tools, side effects, background execution или сабагентом MUST иметь Task.
- Non-blocking запуск использует A2A `return_immediately`; клиент затем polling, subscription или push notification.
- Закрытие streaming connection MUST NOT отменять Task.
- Агент MAY перейти в пассивное `WAITING_TASK`; это остаётся A2A `working`, не потребляет model/CPU и возобновляется notification-ом.
- Critical state не полагается только на transient Message: она сохраняется в Task status/history или Artifact.

## Artifacts и Messages

- Итоговые и промежуточные результаты задачи публикуются как Artifacts с content parts, media type, digest и provenance.
- Messages используются для общения, progress summary, input/approval request и ответа человека.
- Partial artifact updates идемпотентны и имеют stable artifact ID/version.
- Secret, hidden reasoning и raw sensitive tool output MUST NOT попадать в Message или Artifact без явной data policy.

## Notifications

A2A streaming, polling и push notifications являются тремя представлениями одной durable task history. Push delivery MUST быть at-least-once; receiver обрабатывает update идемпотентно по task ID, artifact/status version и event sequence.

Webhook implementation MUST проверять HTTPS/authentication, защищаться от SSRF и не разрешать private/loopback target без явной host policy.

## Trace context

A2A binding MUST принимать и передавать W3C Trace Context через разрешённые transport headers. Trace context не используется для authorization. Baggage по умолчанию не пересылается внешнему агенту; allowlist запрещает secrets, prompt и user content.

## Версионирование

- A2A protocol version договаривается стандартным способом binding-а; runtime фиксирует `Major.Minor`, а patch не участвует в compatibility negotiation.
- Core extension version меняется независимо.
- Новый optional extension field обратно совместим; новый required field требует новой major extension version.
- Persisted Task хранит A2A и extension versions, с которыми он был создан.
