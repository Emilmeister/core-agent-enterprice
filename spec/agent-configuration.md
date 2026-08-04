# Конфигурация агента

## Цель

AgentConfig создаёт конкретный экземпляр Core Agent из общего runtime. Он максимально гибко уменьшает доступные возможности: может отключить memory, terminal, filesystem mutations, delegation, background tasks, отдельные built-in/MCP tools или skills.

Конфигурация не является model prompt и не передаётся заново в каждой A2A Task.

## Три уровня

1. **PlatformConfig** — providers, credentials, stores, execution backends, tenant policy и infrastructure limits.
2. **AgentConfig** — identity/profile, model route, feature switches и capability filters конкретного агента.
3. **Task input** — только `prompt` через A2A Message.

Нижний уровень MAY дополнительно сузить capabilities, но не расширяет верхний.

`agent.profile_prompt` optional и по умолчанию пуст. Он задаёт роль/стиль, но не повторяет Task prompt: фактический запрос передаётся отдельным user Message.

## Model reasoning

`THINKING_LEVEL` является optional deployment setting. Допустимый provider-neutral vocabulary: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`; конкретный provider/model MAY поддерживать подмножество и возвращает model error для неподдерживаемого значения. Пустое значение означает provider default. Эта настройка не входит в RunRequest и не может быть изменена prompt-ом.

OpenAI-compatible adapter передаёт effort как `reasoning_effort`; для MiniMax он также запрашивает отделение reasoning от публичного `content`. Anthropic adapter передаёт effort как `output_config.effort` и, если caller не задал более точный provider config, включает adaptive thinking. Explicit `THINKING_LEVEL` имеет приоритет над совпадающим полем в `LLM_EXTRA_BODY_JSON`.

Reasoning text имеет те же privileged capture boundaries, что остальной model content: stdout требует `CORE_AGENT_LOG_CONTENT=true`, Phoenix/OTel — `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`. Оба значения безопасно выключены по умолчанию production runtime-ом.

## Пример

```yaml
schema_version: v1alpha1
agent:
  name: coding-agent
  profile_prompt: You are a repository coding agent.
model:
  route: coding-default
features:
  background_tasks: true
  delegation: true
  memory: optional
tools:
  builtins:
    default: deny
    allow:
      - core.terminal.exec
      - core.terminal.write
      - core.fs.apply_patch
      - core.task.*
      - core.delegate
    deny: []
  mcp:
    default: deny
    allow_servers: [repo, memory]
    allow_tools:
      repo: [search, read_file]
      memory: [search, read, create, update, split]
skills:
  default: deny
  allow: [database-review, release-notes]
context:
  compact_at_working_ratio: 0.90
  compact_to_working_ratio: 0.15
execution:
  runtime_mode: with_terminal
  environment_profile: local-pty
observability:
  otel_profile: production
budgets:
  depth: 2
```

## Production persistence

Database credentials являются Platform/deployment config и никогда не входят в RunRequest/AgentConfig. Production container требует runtime credential `DATABASE_URL=postgresql://...` из secret injection, использует bounded connection pool и owner/tenant-scoped PostgreSQL rows. Отдельный migration job MAY получать DDL credential через `DATABASE_MIGRATION_URL` и exact runtime role через `DATABASE_APP_ROLE`; serving process не читает migration credential. `DATABASE_POOL_MIN`, `DATABASE_POOL_MAX`, `DATABASE_CONNECT_TIMEOUT_SECONDS` и `DATABASE_AUTO_MIGRATE` управляются deployment-ом.

Пул MUST проверять соединение перед выдачей и заменять непригодное. Managed PostgreSQL и сетевые посредники закрывают простаивающие соединения молча, поэтому без проверки первый запрос после паузы падает с транспортной ошибкой, хотя база доступна. Отказ MUST NOT доходить до caller как сбой задачи: замена соединения относится к обслуживанию пула, а не к семантике запроса.

`DATABASE_AUTO_MIGRATE=false` обязателен production: migration job с DDL role выполняется до app rollout, выдаёт app role только необходимые DML grants, затем app role проверяет schema version. Production startup MUST reject `DATABASE_AUTO_MIGRATE=true`. Test profile MAY явно выбрать SQLite/in-memory adapter; implicit fallback при отсутствии PostgreSQL запрещён.

Production deployment MUST задавать `DURABLE_STORAGE_ROOT` как путь к отдельному S3-backed mount для immutable blobs, snapshots, manifests и artifacts. `LOCAL_WORKSPACE_ROOT` MUST указывать на локальную ephemeral filesystem container-а и не может находиться внутри durable mount. Optional `LOCAL_BASE_SNAPSHOT` содержит content-addressed snapshot ID; если он задан, startup проверяет commit manifest и все blobs до первого terminal call. Active workspace никогда не размещается под `DURABLE_STORAGE_ROOT`.

## Feature switches

Минимальные optional features:

- `memory`: `disabled`, `optional`, `required`;
- `background_tasks`: boolean;
- `delegation`: boolean;
- `terminal`: boolean или результат tool filters;
- `python`: boolean или результат tool filters;
- `filesystem_mutation`: boolean или результат tool filters;
- `mcp`: boolean;
- `skills`: boolean;
- `human_input`: boolean;

Этот runtime не содержит human-in-the-loop: отдельного пути подтверждения side effect человеком нет, а вердикт policy является окончательным. Граница возможностей задаётся исключительно tool allowlist, runtime mode и изоляцией контейнера.

`disabled` memory означает:

- Memory MCP descriptor отклоняется или фильтруется по `required` semantics;
- memory tools отсутствуют в discovery/model context;
- memory-specific kernel policy не загружается;
- Core Agent не выполняет implicit memory retrieval/write.

`optional` разрешает Task передать Memory MCP. `required` требует подходящий descriptor до первого model turn.

## Deployment variables

Deployment передаёт конфигурацию переменными окружения. Ниже перечислен полный контракт: имя, значение по умолчанию и нормативная семантика. Пустая строка означает «не задано» и MUST трактоваться как отсутствие значения, а не как пустое значение. Числовая переменная, не разбирающаяся в число, завершает startup с `CONFIG_INVALID`. Булева переменная принимает `true`/`false` без учёта регистра. Переменная-список разделяется запятыми, пробелы вокруг элементов отбрасываются.

Переменные, не перечисленные здесь, документированы в своих разделах: `CORE_AGENT_*` — runtime modes, budgets и tool allowlist ниже по этому документу; `DATABASE_*` и `PUSH_NOTIFICATION_ENCRYPTION_KEY` — production persistence выше и [A2A protocol](a2a-protocol.md); `MEMORY_*` — [Memory MCP Service](memory-service.md).

### Identity и Agent Card

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `AGENT_NAME` | `core-agent` | Имя в Agent Card; оно же `app_name` в ключах артефактов |
| `AGENT_DESCRIPTION` | `Policy-enforced core agent runtime` | Описание в Agent Card |
| `AGENT_VERSION` | `1.0.0` | Версия в Agent Card |
| `AGENT_SYSTEM_PROMPT` | пусто | AgentProfilePrompt; не повторяет Task prompt и не выдаёт capability |
| `AGENT_URL` | выводится из заголовков запроса | Advertised base URL карточки. Заданное значение authoritative; пустое означает вывод из `X-Forwarded-*`/`Host` на каждый запрос карточки |
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8000` | Bind port |
| `LOG_LEVEL` | `INFO` | Уровень structured stdout |

### Модель

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `LLM_MODEL` | — | Обязательна; отсутствие завершает startup с `CONFIG_INVALID` |
| `LLM_API_FORMAT` | `openai` | `openai` или `anthropic`; определяет request/response и разбор потока |
| `LLM_API_BASE` | пусто | Базовый URL провайдера |
| `LLM_ENDPOINT` | пусто | Полный URL, если он не выводится из базового |
| `LLM_API_KEY` | пусто | Provider credential; не попадает в модель, audit и telemetry |
| `LLM_PROVIDER` | автоопределение | Метка провайдера для OpenInference |
| `LLM_MAX_TOKENS` | `4096` | Верхняя граница ответа |
| `LLM_CONTEXT_WINDOW` | `128000` | Окно модели для расчёта контекстного бюджета |
| `LLM_TOKEN_CHARS` | `3` | Символов на токен в оценке занятости контекста |
| `LLM_TIMEOUT` | `120` | Таймаут запроса в секундах |
| `LLM_TEMPERATURE`, `LLM_TOP_P`, `LLM_TOP_K`, `LLM_FREQUENCY_PENALTY`, `LLM_PRESENCE_PENALTY` | provider default | Пустое значение MUST NOT отправляться в запросе. Anthropic не принимает frequency/presence penalties и отклоняет их при startup |
| `LLM_HEADERS_JSON` | `{}` | Дополнительные заголовки запроса |
| `LLM_EXTRA_BODY_JSON` | `{}` | Дополнительные поля тела; explicit `THINKING_LEVEL` имеет приоритет над совпадающим полем |
| `THINKING_ENABLED` | `true` | `false` MUST отправлять явный effort `none` |
| `THINKING_LEVEL` | provider default | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` |

### MCP

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `MCP_URL` | пусто | Список Streamable HTTP серверов, принадлежащих deployment. Имя сервера берётся из последнего сегмента пути, иначе из hostname, иначе `mcp_{N}`. Зарезервированное имя `memory` дополнительно получает role `memory` — та же конвенция, что и в `MCP_ALLOWED_SERVERS` |
| `MCP_ALLOWED_SERVERS` | `memory` | Allowlist серверов; серверы из `MCP_URL` добавляются автоматически |
| `MCP_ALLOWED_TOOLS` | шесть memory-тулов | Allowlist имён тулов. Голое имя (`get_forecast`) разрешает тул на любом подключённом сервере; форма `server.tool` ограничивает его одним сервером. Тул, отсутствующий в каталоге сервера, просто не появляется |
| `MCP_HEADERS_JSON` | `{}` | Заголовки исходящих MCP-вызовов |
| `MCP_TIMEOUT` | `30.0` | Таймаут одного вызова, секунды |
| `MCP_SSE_READ_TIMEOUT` | `300.0` | Таймаут чтения event stream, секунды |

### Удалённые A2A-агенты

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `REMOTE_AGENTS` | пусто | Список базовых URL. Пустой список MUST удалять `core.agent.send_message` из карточки и каталога |
| `REMOTE_AGENTS_TIMEOUT` | `15.0` | Таймаут загрузки карточки и вызова, секунды |
| `REMOTE_AGENTS_MAX_RETRIES` | `3` | Повторы загрузки карточки при retryable-ошибке |
| `REMOTE_AGENTS_RETRY_DELAY` | `1.0` | Базовая задержка повтора, секунды |
| `REMOTE_AGENTS_RETRY_BACKOFF` | `2.0` | Множитель задержки; MUST быть не меньше `1` |
| `REMOTE_AGENTS_RETRYABLE_STATUS_CODES` | `500,502,503,504` | Дополнительные retryable статусы поверх встроенных 408 и 429 |
| `SEND_MESSAGE_API_KEY` | пусто | Непустое значение заменяет проксируемый `Authorization` на `Api-Key <значение>` |

Полный контракт описан в разделе [Удалённые A2A-агенты](tasks-and-delegation.md#удалённые-a2a-агенты).

### A2A и streaming

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `A2A_CAPABILITIES` | `streaming,push_notifications,tool_calling,multi_turn` | MAY только сужать реально реализованное. Неизвестное значение завершает startup с `CONFIG_INVALID`. На Agent Card влияют только `streaming` и `push_notifications`; остальные два принимаются для совместимости схемы |
| `A2A_STREAMING_ENABLED` | `true` | Выключение убирает промежуточные кадры и отключает streaming-режим запроса к модели |
| `A2A_STREAMING_BUFFER_SIZE` | `10` | Символов роста до следующего кумулятивного снимка |

### Runtime и лимиты ответа

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `RUNTIME_MAX_LLM_CALLS` | `100` | Hard limit числа model turns |
| `USER_ID` | `anonymous` | Identity, когда A2A-вызов не аутентифицирован |
| `MAX_RESPONSE_SIZE` | `50000000` | Верхняя граница одного артефакта в байтах |
| `MAX_CHUNK_SIZE` | `0` | Символов в одном чанке A2A Artifact; `0` означает один чанк |
| `ENTITY_ID` | пусто | Добавляется заголовком `X-Internal-Entity-ID` к исходящим provider и MCP вызовам |
| `RUNTIME_SAVE_INPUT_BLOBS_AS_ARTIFACTS` | `false` | `true` сохраняет входящие binary Parts как артефакты сессии и реферирует их в prompt; `false` отклоняет их `CONTENT_TYPE_NOT_SUPPORTED` |

### Плагины

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `REFLECT_AND_RETRY_ENABLED` | `true` | Включает bounded retry вызова модели |
| `REFLECT_AND_RETRY_MAX_RETRIES` | `3` | Число повторов; повтор допустим только для retryable provider error, задержка `2^attempt` секунд с потолком 8 |
| `EVENTS_COMPACTION_ENABLED` | `true` | Включает compaction истории |
| `EVENTS_COMPACTION_INTERVAL` | `0` | Turns между принудительными compaction; `0` означает compaction только по достижении контекстного порога |
| `EVENTS_COMPACTION_OVERLAP_SIZE` | `0` | Сколько последних unpinned элементов сохраняется дословно рядом с summary |
| `CONTEXT_CACHE_ENABLED` | `true` | Включает prompt caching |
| `CONTEXT_CACHE_MIN_TOKENS` | `2048` | Минимальный размер кэшируемого префикса |
| `CONTEXT_CACHE_TTL_SECONDS` | `600` | Значение больше `300` выбирает `1h`, иначе `5m` |

Prompt caching является полем запроса только у Anthropic. OpenAI-совместимые провайдеры кэшируют префикс автоматически и игнорируют эти значения.

### Хранилища

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `SESSION_STORAGE_TYPE` | `postgres` в production и при заданной базе, иначе `in-memory` | Допустимы ровно `in-memory` и `postgres`. Production MUST использовать `postgres` |
| `SESSION_DATABASE_URL` | пусто | Полный URL; имеет приоритет над частями ниже |
| `SESSION_POSTGRES_PROTOCOL` | `postgresql` | Схема собираемого URL |
| `SESSION_POSTGRES_USER`, `SESSION_POSTGRES_PASSWORD` | пусто | Credentials; percent-кодируются при сборке URL |
| `SESSION_POSTGRES_HOST` | пусто | Непустое значение включает сборку URL из частей |
| `SESSION_POSTGRES_PORT` | `5432` | Порт собираемого URL |
| `SESSION_POSTGRES_DATABASE` | пусто | Имя базы собираемого URL |
| `TASK_STORAGE_TYPE` | значение `SESSION_STORAGE_TYPE` | Допустимы ровно `in-memory` и `postgres`. `postgres` при не-postgres сессиях завершается `CONFIG_CONFLICT` |
| `TASK_POSTGRES_URL` | пусто | Fallback URL перед `DATABASE_URL` |
| `ARTIFACT_STORAGE_TYPE` | `in-memory` | `in-memory`, `s3` или `mongodb` |
| `ARTIFACT_S3_*`, `ARTIFACT_MONGODB_URL` | см. [Артефакты](artifacts.md) | Параметры backend-интеграций |

Порядок разрешения URL сессионной базы фиксирован: `SESSION_DATABASE_URL`, затем сборка из `SESSION_POSTGRES_*` при непустом host, затем `TASK_POSTGRES_URL`, затем `DATABASE_URL`.

Artifact backends `s3` и `mongodb` являются интеграциями: они читают и пишут объекты, но не создают и не мигрируют схему хранилища. Naming, scope и versioning живут внутри агента и описаны в [Артефактах](artifacts.md).

### Telemetry

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `OTEL_ENDPOINT` | пусто | Базовый OTLP endpoint; per-signal `OTEL_EXPORTER_OTLP_*_ENDPOINT` имеют приоритет |
| `OTEL_API_KEY` | пусто | Передаётся заголовком экспортёра |
| `OTEL_SERVICE_NAME` | `core-agent` | Имя сервиса в трассировке |

## Runtime modes

Deployment MUST выбрать один из двух capability-профилей через `CORE_AGENT_RUNTIME_MODE`:

- `with_terminal` — разрешает `core.terminal.exec` и `core.task.start`;
- `without_terminal` — удаляет `core.terminal.exec` и `core.task.start`, но сохраняет task lifecycle (`get`, `list`, `wait`, `cancel`), delegation, MCP, memory и Python.

Значение по умолчанию — `with_terminal`. Неизвестное значение завершает startup с `CONFIG_INVALID`. Runtime mode является верхней границей capabilities: `CORE_AGENT_ALLOWED_BUILTIN_TOOLS`, AgentConfig, Task и delegation contract могут только сузить выбранный профиль. В частности, старый allowlist с terminal tools не может снова включить их в `without_terminal`.

`without_terminal` означает отсутствие model-visible terminal capability, а не OS security sandbox: Python-код всё ещё может использовать стандартные `os`, `subprocess` и filesystem APIs внутри доверенной single-container среды. Shell, stdio MCP и skill scripts не предоставляются как самостоятельные tools в этом профиле. `core.python.exec` управляется только built-in allowlist.

## Tool filters

Built-in и MCP tools фильтруются после discovery, но до model context:

1. Platform/tenant deny policy;
2. AgentConfig feature switch;
3. AgentConfig server/tool allow/deny;
4. Task-provided MCP/skills;
5. delegation allowlist для child.

На каждом уровне deny имеет приоритет. Wildcard разрешён только в namespaced форме вроде `core.task.*` или `memory.*`; глобальный `*` SHOULD быть запрещён production policy.

Отключённый tool:

- не показывается модели;
- не может быть вызван по старому имени;
- не наследуется child Task;
- возвращает `CAPABILITY_DISABLED`, если вызов восстановлен из stale model output.

Protocol-internal A2A state transitions, policy checks, audit/redaction и ownership/lifecycle TerminalSession не являются model-callable tools и не отключаются tool filters.

Config validation MUST обнаруживать как минимум:

- `delegation: true` при отключённых background tasks или `core.delegate`;
- `memory: required`, если policy не разрешает ни одного Memory MCP server/tool;
- advertised A2A capability без runtime/transport implementation;
- tool allow pattern, полностью перекрытый deny policy;
- skill/MCP requirement, несовместимый с execution/network profile.

`budgets.depth` задаёт максимальную глубину сабагентов относительно main agent с depth `0`. Допустимы только целые значения `0`, `1` и `2`; hard platform maximum `2` не может быть увеличен через deployment environment, AgentConfig или delegation contract.

## MCP policy и roles

MCP descriptor MAY иметь host-validated role, например `memory`, `repository` или `issue_tracker`. Role не доверяется только потому, что пришла от клиента: AgentConfig/tenant policy сверяет server identity, transport target и optional integrity metadata.

Для каждого server можно настроить:

- allowed transports/targets;
- required/optional behavior;
- capability types tools/resources/prompts/sampling/elicitation;
- tool allow/deny patterns;
- secret refs;
- network/execution profile;
- trusted capability policy profile.

Memory mode относится только к MCP server с подтверждённой role `memory`.

## Skills policy

AgentConfig задаёт allowed sources, names, versions, permissions и default deny/allow. Task передаёт желаемые skills, но effective catalog содержит только пересечение Task request и AgentConfig/tenant policy.

## EffectiveConfig snapshot

Перед A2A Task runtime вычисляет immutable EffectiveConfig:

```text
EffectiveConfig = PlatformConfig ∩ tenant policy ∩ AgentConfig ∩ Task capabilities
```

Snapshot содержит versions/digests profile prompt, kernel policy packs, model route, tool schemas, MCP/skill locks, budgets, context thresholds и execution/OTel profiles.

- Unknown config fields MUST отклоняться.
- Conflict MUST возвращать path и безопасную причину.
- Hot reload применяется только к новой Task, кроме security revocation.
- Agent Card генерируется из Effective AgentConfig и не рекламирует disabled capability.
- Audit/OTel записывают config version/digest без secret values.

## Границы гибкости

AgentConfig может отключить функциональность, но не может:

- адресовать TerminalSession или process другого agent/run;
- отключить schema validation, tenant isolation или secret redaction;
- раскрыть raw chain-of-thought;
- обойти policy для оставшегося tool;
- объявить A2A capability, которой runtime фактически не поддерживает;
- превратить AgentProfilePrompt в capability grant.

## Ошибки конфигурации

- `CONFIG_INVALID` — schema/type/value invalid;
- `CONFIG_CONFLICT` — взаимоисключающие switches/policies;
- `CAPABILITY_DISABLED` — Task запросила отключённую capability;
- `REQUIRED_CAPABILITY_MISSING` — required memory/MCP/skill отсутствует;
- `TOOL_FILTER_EMPTY` — required workflow не имеет ни одного разрешённого tool.

## Отказ при старте

Невалидная конфигурация является ошибкой оператора, а не дефектом runtime. Она MUST обрабатываться fail closed и MUST оставаться диагностируемой из одних только логов процесса.

Serving entrypoint MUST:

- прекратить запуск: HTTP listener, A2A routes и health endpoints MUST NOT подниматься. Частично собранный процесс запрещён, потому что зелёный readiness при неработающей модели превращает одну понятную ошибку старта в непрозрачный отказ на каждом A2A-вызове;
- завершиться ненулевым кодом возврата, чтобы оркестратор считал запуск неуспешным;
- вывести ровно одну строку с безопасной причиной и стабильным кодом ошибки;
- не печатать Python traceback: он не добавляет оператору информации о том, какую переменную править.

Сообщение MUST называть конкретную настройку, а не только класс ошибки. `LLM_API_FORMAT must be openai or anthropic` является достаточным, голый `CONFIG_INVALID` — нет. Значения secret-переменных MUST NOT попадать в это сообщение; допустимо назвать только имя переменной.

Требование распространяется на все ошибки старта, включая production-gates `PRODUCTION_DATABASE_REQUIRED`, `PRODUCTION_AUTO_MIGRATE_FORBIDDEN`, `DURABLE_STORAGE_REQUIRED` и `PUSH_ENCRYPTION_KEY_REQUIRED`.

Ошибка конфигурации, обнаруженная после успешного старта, остаётся обычной runtime-ошибкой и не завершает процесс.
