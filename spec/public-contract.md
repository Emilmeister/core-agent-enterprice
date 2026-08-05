# Публичный контракт

Основной внешний protocol — [A2A](a2a-protocol.md). Этот документ определяет Core Agent payload и semantics поверх A2A, а не отдельный HTTP/RPC protocol.

## Логический RunRequest

Внутреннее ядро получает ровно один пользовательский вход:

```json
{"prompt": "Исправь падающий тест и объясни причину"}
```

A2A adapter строит его из `Message.parts`. Никакого собственного расширения протокола для этого не требуется: любой стандартный A2A-клиент является валидным клиентом Core Agent.

MCP-серверы и skills MUST задаваться конфигурацией развёртывания и MUST NOT приниматься из запроса. Прежняя передача их через versioned extension удалена: она делала обязательным собственное расширение на каждом запросе и тем самым отсекала стандартные A2A-клиенты, не давая взамен ничего, что нельзя выразить конфигурацией.

Session/tenant/auth/trace/task delivery принадлежат A2A context и transport security. Они не становятся вторым полем RunRequest.

## `prompt`

- Непустой text Part является обязательным shorthand.
- Целевой контракт принимает A2A text, file/artifact reference и structured data Parts согласно advertised media types.
- Бинарные данные SHOULD передаваться ссылкой/content-addressed artifact, а не inline base64.
- Message содержит один новый пользовательский turn. История A2A context/session добавляется ядром.
- Исходные Parts, role, IDs и digests сохраняются на протяжении Task.
- Неподдерживаемая modality отклоняется стандартной A2A content-type ошибкой до model turn.
- Ни один принятый Part MUST NOT быть отброшен молча. Part, который runtime не может ни передать модели, ни сохранить, MUST завершаться `CONTENT_TYPE_NOT_SUPPORTED`; тихое игнорирование запрещено, потому что caller получает успешный ответ на запрос, часть которого не рассматривалась.

### Бинарные Parts

Модель принимает текст, поэтому inline-байты MUST NOT попадать в prompt. Входящий binary Part обрабатывается только одним способом: он сохраняется как artifact текущей сессии, а в prompt добавляется ссылка на сохранённое имя и версию. Дальше модель работает с ним через artifact tools; полный контракт именования и scope определён в [Артефактах](artifacts.md).

Поведение управляется `RUNTIME_SAVE_INPUT_BLOBS_AS_ARTIFACTS`:

- `true` — binary Part сохраняется и реферируется в prompt;
- `false` (по умолчанию) — binary Part отклоняется `CONTENT_TYPE_NOT_SUPPORTED`.

Значение по умолчанию сохраняет исходную семантику схемы переменных, но в этом runtime оно означает отказ, а не «файл принят, но не сохранён»: без сохранения модель физически не получила бы содержимое.

Part, передающий данные ссылкой (URL), MUST отклоняться `CONTENT_TYPE_NOT_SUPPORTED` независимо от флага. Runtime не выполняет исходящий запрос по адресу, который выбрал caller: это server-side request forgery от имени агента и его сетевых прав.

## `mcp`

MCP connection descriptors принадлежат конфигурации развёртывания: Streamable HTTP servers объявляются `MCP_URL`, а allowlist — `MCP_ALLOWED_SERVERS`/`MCP_ALLOWED_TOOLS`. Запрос их не передаёт и не может расширить. Пустая конфигурация означает отсутствие MCP capabilities.

Формат descriptor остаётся прежним; целевой продукт поддерживает stdio и Streamable HTTP:

```json
{
  "name": "repo-tools",
  "required": true,
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
  "required": true,
  "transport": {
    "type": "streamable_http",
    "url": "https://mcp.example.test",
    "header_refs": {"Authorization": "secret/issues-auth"}
  }
}
```

Требования:

- `name` уникален внутри развёртывания;
- descriptor MAY запрашивать tools/resources/prompts/sampling/elicitation;
- secrets передаются ссылками на host secret store;
- descriptor проходит allowlist до подключения;
- stdio MCP запускается как owned process в TerminalSession текущего agent-а;
- catalog фиксируется snapshot-ом; notification меняет revision только на safe boundary;
- ошибка `required: true` завершает Task, optional descriptor создаёт наблюдаемый warning.

Optional server объявляется тем же descriptor с `required: false`:

```json
{
  "name": "docs-search",
  "required": false,
  "transport": {
    "type": "streamable_http",
    "url": "https://docs.example.test/mcp"
  }
}
```

Объявление само по себе не делает server доверенным. PlatformConfig/AgentConfig должны подтвердить identity/target. Отфильтрованный конфигурацией descriptor не попадает в effective catalog: `required: true` завершает validation с `CAPABILITY_DISABLED`, optional descriptor создаёт наблюдаемый filtered-capability warning.

## `skills`

Skill package references принадлежат конфигурации развёртывания и задаются `CORE_AGENT_ALLOWED_SKILLS`. Запрос их не передаёт. Пустая конфигурация означает отсутствие пользовательских runtime skills.

```json
{
  "name": "release-notes",
  "source": "skill://company/release-notes@2.1.0",
  "integrity": "sha256-..."
}
```

- `name` уникален внутри развёртывания;
- source resolver разрешён host policy;
- remote immutable source имеет integrity/signature provenance;
- dependency graph фиксируется lock snapshot-ом;
- package проходит [Skills](skills.md) validation;
- A2A Agent Skill в Agent Card и этот runtime skill descriptor являются разными сущностями.

## Совместимость с прежним расширением

Agent Card MUST NOT объявлять `urn:core-agent:run-capabilities:v1` ни required, ни optional. Клиент, продолжающий присылать это расширение, MUST обслуживаться как обычно: и заголовок `A2A-Extensions`, и `Message.extensions`, и payload в metadata игнорируются.

Исключение обязательно: если payload содержит непустые `mcp` или `skills`, запрос MUST отклоняться `CONFIG_INVALID` с указанием, что эти возможности теперь задаются конфигурацией. Молчаливое игнорирование здесь означало бы, что caller получил успешный ответ на задачу, решённую без переданных им серверов и skills.

## Task и background mode

- Простая безопасная задача без tracking MAY вернуть direct A2A Message.
- Любая Task с tools, background work, memory write или сабагентом MUST вернуть A2A Task.
- `return_immediately` включает non-blocking mode: сервер подтверждает Task и продолжает её в фоне.
- Клиент получает updates через get/list, subscribe/stream или push notifications.
- Закрытие stream не отменяет Task.
- Main agent также использует этот lifecycle для внутренних background/subagent tasks.

## Неполное завершение по budget

Исчерпание execution budget не меняет форму запроса и не вводит новый A2A
terminal state. Runtime публикует обычный text Artifact и завершает Task как
`completed`, но Artifact/result metadata содержит
`completion_reason: "budget_exhausted"`, `complete: false` и исчерпанную
dimension. Сам текст явно отделяет проверенный промежуточный результат от того,
что агент намеревался, но не успел выполнить; отсутствующие tool outcomes не
достраиваются.

Result metadata также содержит локальный `usage`, атомарный snapshot общего
root ledger `shared_budget` и, если cancel не подтвердился за bounded grace,
`pending_tasks`. Live delivery и recovery после crash публикуют эти поля в одной
и той же `provenance` metadata Artifact-а.

Это аддитивное обратно совместимое поле: клиент, не читающий metadata, всё равно
получает самодостаточный текст о неполноте. Отсутствие этих полей в ранее
сохранённом результате означает `completion_reason: "completed"` и
`complete: true`; новая версия публичного binding или миграция JSON rows не
требуются.

## Follow-up в активную Task

Клиент не обязан ждать завершения Task, чтобы написать снова. Follow-up Message указывает существующий `taskId` и тот же `contextId`; A2A adapter сохраняет его в durable inbox текущего run и возвращает ту же Task. `contextId` без `taskId` начинает отдельную Task в той же conversation context.

Follow-up содержит новый text turn. Он не может изменить EffectiveConfig, tenant, model route, policy или budgets активной Task. Повторный `messageId` идемпотентен; concurrent Messages упорядочиваются server-side sequence.

Runtime добавляет принятый input в model transcript как user-role Message на ближайшей safe boundary. Уже выполняющийся model/tool call не прерывается. Перед завершением Task runtime обязан атомарно проверить, что нет более раннего непрочитанного input. После terminal state продолжение диалога создаёт новую Task в том же `contextId`.

## Результаты

Основной результат Task — A2A Artifact:

- stable artifact ID и revision;
- один или несколько typed Parts;
- media type, size и digest;
- provenance на Task/tool/memory revision;
- `append`/`lastChunk` semantics для streaming, если поддерживаются binding version.

User-facing progress и requests передаются Messages/Task status. Critical result не хранится только в transient status Message.

## Cancellation и passive wait

- Внешняя отмена использует A2A cancel Task operation.
- Внутренний agent может вызвать `core_task_cancel` для child/background Task.
- Passive wait не создаёт внешней terminal state: Task остаётся `working`, status metadata сообщает `waiting_task`.
- Task, ожидающая notification, не удерживает model worker, active terminal process или busy loop.

## Ordering и idempotency

Internal event log имеет monotonic revision/sequence. A2A status/artifact updates содержат достаточную version metadata, чтобы:

- восстановить порядок после reconnect;
- дедуплицировать at-least-once push delivery;
- не применить stale input;
- не перепутать artifact chunks;
- связать внешний Task с внутренним audit.

Inbound Messages используют тот же принцип: `(task_id, message_id)` уникален, а committed inbox sequence задаёт порядок model delivery. Inbox append и terminal transition сериализуются так, чтобы сервер не подтвердил Message, которое Task затем потеряет.

## Terminal semantics

Каждая Task достигает ровно одного A2A terminal state: `completed`, `failed`, `canceled` или `rejected`. Внутренний `ABORTED` отображается в `failed` с безопасным `unsafe_continuation` reason.

После terminal state новые Messages этой Task отклоняются стандартной A2A terminal-task ошибкой. Продолжение диалога создаёт новую Task в том же `contextId`.

## Embedded SDK

Embedded/local SDK MAY предоставить convenience `run(prompt, mcp, skills)` и typed event iterator. Он MUST:

- использовать ту же A2A Task/message/artifact semantics;
- отдавать Agent Card/capability metadata;
- не создавать другой lifecycle;
- позволять поднять A2A binding без изменения domain behavior.

## Совместимость

- Добавление optional A2A status metadata обратно совместимо.
- Persisted Task хранит обе versions для replay/migration.
- Live steering использует стандартные `Message.taskId/contextId/messageId` и не добавляет полей к контракту.
