# Публичный контракт

Версия Core Agent extension: `v1alpha1` до первой стабильной реализации.

Основной внешний protocol — [A2A](a2a-protocol.md). Этот документ определяет Core Agent payload и semantics поверх A2A, а не отдельный HTTP/RPC protocol.

## Логический RunRequest

Внутреннее ядро получает ровно три пользовательских входа:

```json
{
  "prompt": "Исправь падающий тест и объясни причину",
  "mcp": [],
  "skills": []
}
```

A2A adapter строит его так:

| Логический input | A2A representation |
|---|---|
| `prompt` | входной A2A `Message.parts` |
| `mcp` | `urn:core-agent:run-capabilities:v1` structured extension data |
| `skills` | тот же extension payload |

Session/tenant/auth/trace/task delivery принадлежат A2A context и transport security. Они не становятся четвёртым полем RunRequest.

## `prompt`

- Непустой text Part является обязательным shorthand.
- Целевой контракт принимает A2A text, file/artifact reference и structured data Parts согласно advertised media types.
- Бинарные данные SHOULD передаваться ссылкой/content-addressed artifact, а не inline base64.
- Message содержит один новый пользовательский turn. История A2A context/session добавляется ядром.
- Исходные Parts, role, IDs и digests сохраняются на протяжении Task.
- Неподдерживаемая modality отклоняется стандартной A2A content-type ошибкой до model turn.

## `mcp`

Массив MCP connection descriptors. Пустой массив означает отсутствие MCP capabilities в конкретной Task и ничего не наследует из предыдущей Task context-а.

Целевой продукт поддерживает stdio и Streamable HTTP:

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

- `name` уникален внутри Task;
- descriptor MAY запрашивать tools/resources/prompts/sampling/elicitation;
- secrets передаются ссылками на host secret store;
- descriptor проходит allowlist и risk/approval до подключения;
- stdio MCP запускается как owned process в TerminalSession текущего agent-а;
- catalog фиксируется snapshot-ом; notification меняет revision только на safe boundary;
- ошибка `required: true` завершает Task, optional descriptor создаёт наблюдаемый warning.

Memory передаётся обычным MCP descriptor с host-validated role:

```json
{
  "name": "memory",
  "role": "memory",
  "required": false,
  "transport": {
    "type": "streamable_http",
    "url": "https://memory.example.test/mcp"
  }
}
```

`role` не делает server доверенным сама по себе. PlatformConfig/AgentConfig должны подтвердить identity/target. При memory disabled descriptor не попадает в effective catalog: `required: true` завершает validation с `CAPABILITY_DISABLED`, optional descriptor создаёт наблюдаемый filtered-capability warning.

## `skills`

Массив content-addressed skill package references. Пустой массив означает отсутствие пользовательских runtime skills в конкретной Task.

```json
{
  "name": "release-notes",
  "source": "skill://company/release-notes@2.1.0",
  "integrity": "sha256-..."
}
```

- `name` уникален внутри Task;
- source resolver разрешён host policy;
- remote immutable source имеет integrity/signature provenance;
- dependency graph фиксируется lock snapshot-ом;
- package проходит [Skills](skills.md) validation;
- A2A Agent Skill в Agent Card и этот runtime skill descriptor являются разными сущностями.

## Core extension payload

Message использует required A2A extension; HTTP binding также передаёт URI через `A2A-Extensions`:

```json
{
  "extensions": ["urn:core-agent:run-capabilities:v1"],
  "metadata": {
    "urn:core-agent:run-capabilities:v1": {
      "mcp": [],
      "skills": []
    }
  }
}
```

Extension value содержит ровно `mcp` и `skills`. Prompt находится только в Message Parts. Неизвестное обязательное поле требует новой extension version; неизвестное optional поле MAY игнорироваться только по правилам объявленной версии.

## Task и background mode

- Простая безопасная задача без tracking MAY вернуть direct A2A Message.
- Любая Task с tools, background work, approval, memory write или сабагентом MUST вернуть A2A Task.
- `return_immediately` включает non-blocking mode: сервер подтверждает Task и продолжает её в фоне.
- Клиент получает updates через get/list, subscribe/stream или push notifications.
- Закрытие stream не отменяет Task.
- Main agent также использует этот lifecycle для внутренних background/subagent tasks.

## Результаты

Основной результат Task — A2A Artifact:

- stable artifact ID и revision;
- один или несколько typed Parts;
- media type, size и digest;
- provenance на Task/tool/memory revision;
- `append`/`lastChunk` semantics для streaming, если поддерживаются binding version.

User-facing progress и requests передаются Messages/Task status. Critical result не хранится только в transient status Message.

## Human-in-the-loop

Approval или новые данные переводят A2A Task в `input-required` и прикладывают Message:

- `reason: approval_required` содержит approval ID, точный effect, redacted arguments, risks и scope options;
- `reason: information_required` содержит typed response schema;
- authentication использует `auth-required`, а не маскируется под approval.

Клиент отвечает новым Message существующей Task. Approval response использует structured Part:

```json
{
  "type": "urn:core-agent:approval-response:v1",
  "approval_id": "apr_01...",
  "decision": "approve",
  "scope": "single_call"
}
```

Approval request использует `urn:core-agent:approval-request:v1`; response — `urn:core-agent:approval-response:v1`. Оба URI объявляются optional в Agent Card, но HITL client opt-in перечисляет их в binding service parameters. Response Message сохраняет исходные `taskId` и `contextId`, не повторяет `mcp`/`skills` и не создаёт новый RunRequest.

До response Task остаётся interrupted, а pending tool не стартует. `approve` возобновляет сохранённый exact call после повторной policy/digest проверки; `deny` продолжает тот же loop с denied ToolResult. Approval decision не является prompt и не передаётся модели как пользовательская инструкция.

Повторная или устаревшая decision возвращает stable Core error `APPROVAL_ALREADY_RESOLVED`, отображённую в A2A error/status semantics.

## Cancellation и passive wait

- Внешняя отмена использует A2A cancel Task operation.
- Внутренний agent может вызвать `core.task.cancel` для child/background Task.
- Passive wait не создаёт внешней terminal state: Task остаётся `working`, status metadata сообщает `waiting_task`.
- Task, ожидающая notification, не удерживает model worker, active terminal process или busy loop.

## Ordering и idempotency

Internal event log имеет monotonic revision/sequence. A2A status/artifact updates содержат достаточную version metadata, чтобы:

- восстановить порядок после reconnect;
- дедуплицировать at-least-once push delivery;
- не применить stale approval/input;
- не перепутать artifact chunks;
- связать внешний Task с внутренним audit.

## Terminal semantics

Каждая Task достигает ровно одного A2A terminal state: `completed`, `failed`, `canceled` или `rejected`. Внутренний `ABORTED` отображается в `failed` с безопасным `unsafe_continuation` reason.

После terminal state новые Messages этой Task отклоняются стандартной A2A terminal-task ошибкой. Продолжение диалога создаёт новую Task в том же `contextId`.

## Embedded SDK

Embedded/local SDK MAY предоставить convenience `run(prompt, mcp, skills)` и typed event iterator. Он MUST:

- использовать ту же A2A Task/message/artifact semantics;
- отдавать Agent Card/capability metadata;
- не создавать другой lifecycle или approval contract;
- позволять поднять A2A binding без изменения domain behavior.

## Совместимость

- A2A protocol version и Core extension version согласуются независимо.
- Добавление optional A2A status metadata обратно совместимо.
- Изменение смысла `mcp`/`skills`, обязательного поля или approval payload требует новой major extension version.
- Persisted Task хранит обе versions для replay/migration.
