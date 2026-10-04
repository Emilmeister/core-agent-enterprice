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
      - core_terminal_exec
      - core_terminal_write
      - core_fs_apply_patch
      - core_task_*
      - core_delegate
      - core_memory_*
    deny: []
  mcp:
    default: deny
    allow_servers: [repo]
    allow_tools:
      repo: [search, read_file]
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

Production deployment MUST задавать `DURABLE_STORAGE_ROOT` для immutable blobs, snapshots, manifests и artifacts; S3-backed mount допустим. `LOCAL_WORKSPACE_ROOT` MUST указывать на локальную ephemeral filesystem container-а. Авторизованное развёртывание MUST явно задавать `CHAT_WORKSPACE_ROOT` на постоянном POSIX томе. Все три root MUST быть различны и не находиться друг внутри друга; startup отклоняет пересечение в любом направлении. Optional `LOCAL_BASE_SNAPSHOT` содержит content-addressed snapshot ID для ephemeral scratch; если он задан, startup проверяет commit manifest и все blobs до первого terminal call. Он не материализуется поверх постоянной папки чата. Active workspace никогда не размещается под `DURABLE_STORAGE_ROOT`.

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

Enterprise v1 включает owner HITL поверх неизменного platform/tenant/mode ceiling. Новый инструмент по умолчанию требует подтверждения каждого вызова; deny окончателен для этого вызова. Owner UI изменяет текущую policy и guardrails exception по [Инструментам](tools.md), не расширяя ceiling.

`disabled` memory означает:

- backend памяти не создаётся;
- ни один `core_memory_*` tool не попадает в effective catalog и в model context;
- memory-specific kernel policy не загружается;
- Core Agent не выполняет implicit memory retrieval/write.

`optional` включает память, если backend доступен. `required` требует работоспособный backend до первого model turn: его отсутствие является ошибкой старта, а не тихой деградацией.

## Deployment variables

Deployment передаёт конфигурацию переменными окружения. Ниже перечислен полный контракт: имя, значение по умолчанию и нормативная семантика. Пустая строка означает «не задано» и MUST трактоваться как отсутствие значения, а не как пустое значение, кроме явно документированных allowlist-переменных, где пустой список отключает capability. `CORE_AGENT_ALLOWED_SKILLS` является таким исключением. Числовая переменная, не разбирающаяся в число, завершает startup с `CONFIG_INVALID`. Floating-point значения MUST быть конечными: `NaN`, `inf` и `-inf` отклоняются с тем же кодом. Неверный `LOG_LEVEL`, неизвестные `TASK_STORAGE_TYPE`/`A2A_CAPABILITIES` и нечисловой элемент списка HTTP retry codes также завершают startup с `CONFIG_INVALID`. Эти ошибки называют настройку, но не содержат введённого значения или traceback; listener не запускается. Fatal startup diagnostic MUST быть видимым при любом поддерживаемом `LOG_LEVEL`, включая `CRITICAL`/`FATAL`. Булева переменная принимает `true`/`false` без учёта регистра. Переменная-список разделяется запятыми, пробелы вокруг элементов отбрасываются.

Переменные, не перечисленные здесь, документированы в своих разделах: `CORE_AGENT_*` — runtime modes, budgets и tool allowlist ниже по этому документу; `DATABASE_*` и `PUSH_NOTIFICATION_ENCRYPTION_KEY` — production persistence выше и [A2A protocol](a2a-protocol.md); остальные `MEMORY_*` — [Память агента](memory-service.md).

### Identity и Agent Card

Production enterprise authentication требует complete configuration ниже.
Частичная configuration отклоняется; настроенная auth не переходит в legacy
режим при ошибке Keycloak. Полностью отсутствующая configuration разрешает
legacy routes только в явно выбранном development/test profile.

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `CORE_AGENT_ENVIRONMENT` | обязательна | Ровно `production`, `development` или `test`; отсутствие и неизвестное значение завершают startup с `CONFIG_INVALID` |
| `CORE_AGENT_TENANT_ID` | обязательна в enterprise | Trusted company scope, не берётся из prompt/metadata |
| `KEYCLOAK_ISSUER_URL` | обязательна | Realm issuer, HTTPS; loopback HTTP только development/test |
| `KEYCLOAK_CLIENT_ID` | обязательна | Confidential introspection client |
| `KEYCLOAK_CLIENT_SECRET` | обязательна | Secret introspection credential; не сохраняется в task/model/audit |
| `KEYCLOAK_UI_CLIENT_ID` | пусто | Отдельный public browser client для Code Flow + PKCE S256. Непустое значение включает публичный bootstrap `/ui/config`; не совпадает с introspection client ID, длина до 255 символов, без whitespace/control characters. Требует полной Keycloak configuration; пустое значение оставляет bootstrap закрытым |
| `KEYCLOAK_AUDIENCE` | обязательна | Expected audience и scope принимаемых client roles |
| `KEYCLOAK_OWNER_ROLE` | `agent-owner` | Роль владельца |
| `KEYCLOAK_EXTERNAL_ROLE` | `agent-external` | Роль внешнего caller; исключает owner/HITL authority |

Новый HTTP-запрос проверяется introspection без auth cache; уже открытый ответ
не переавторизуется. `/a2a/owner`, `/a2a/external` и `/api` используют эти
границы; `/health/live` и `/health/ready` остаются public probes.

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `AGENT_NAME` | `core-agent` | Имя в Agent Card |
| `AGENT_DESCRIPTION` | `Policy-enforced core agent runtime` | Описание в Agent Card |
| `AGENT_VERSION` | `1.0.0` | Версия в Agent Card |
| `AGENT_SYSTEM_PROMPT` | пусто | AgentProfilePrompt; не повторяет Task prompt и не выдаёт capability |
| `AGENT_URL` | выводится из заголовков запроса | Advertised base URL карточки. Заданное значение authoritative; пустое означает вывод из `X-Forwarded-*`/`Host` на каждый запрос карточки. Синоним `URL_AGENT` принимается наравне, `AGENT_URL` имеет приоритет |
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
| `LLM_EXTRA_BODY_JSON` | `{}` | Дополнительные поля тела; explicit `THINKING_LEVEL` имеет приоритет над совпадающим полем. `tools`, `tool_choice`, legacy `functions` и `function_call` из этого объекта игнорируются: каталог задаётся только текущими разрешёнными tools runtime |
| `THINKING_ENABLED` | `true` | `false` MUST отправлять явный effort `none` |
| `THINKING_LEVEL` | provider default | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` |

Guardrails использует отдельный context без tools. Если четыре connection
переменные ниже пусты, используется подключение модели агента с отдельным
непотоковым adapter; настройки основного adapter не меняются. Override требует
все четыре непустых значения; частичный набор даёт `CONFIG_INVALID`.
Override не наследует credential или дополнительные headers основной модели.

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `GUARDRAILS_LLM_PROVIDER` | подключение агента | `anthropic` выбирает Anthropic Messages; остальные provider labels используют OpenAI-compatible binding |
| `GUARDRAILS_LLM_MODEL` | модель агента | Имя отдельной модели детектора |
| `GUARDRAILS_LLM_BASE_URL` | подключение агента | Trusted provider URL; не приходит из prompt или tool arguments |
| `GUARDRAILS_LLM_API_KEY` | подключение агента | Отдельный credential, не передаётся основной модели или в логи |
| `GUARDRAILS_TIMEOUT_SECONDS` | `60` | Положительный конечный предел времени детектора на материал; сохраняется абсолютным deadline |
| `GUARDRAILS_MAX_INPUT_TOKENS` | `100000` | Положительный суммарный input budget с инструкциями и overlap |
| `GUARDRAILS_MAX_CALLS` | `32` | Положительный предел физических попыток на материал, начисляемых до сети |

### MCP

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `MCP_URL` | пусто | Список Streamable HTTP серверов, принадлежащих deployment. Имя сервера берётся из последнего сегмента пути, иначе из hostname, иначе `mcp_{N}` |
| `MCP_ALLOWED_SERVERS` | пусто | Allowlist серверов; серверы из `MCP_URL` добавляются автоматически |
| `MCP_ALLOWED_TOOLS` | пусто | Allowlist имён тулов. Голое имя (`get_forecast`) разрешает тул на любом подключённом сервере; форма `server.tool` ограничивает его одним сервером. Тул, отсутствующий в каталоге сервера, просто не появляется |
| `MCP_READ_ONLY_TOOLS` | пусто | Доверенный список MCP-инструментов только для чтения в формах голого имени и `server.tool`. Scoped-форма применяется только к указанному подключённому серверу и не дублируется как голое имя на остальных. Всё отсутствующее считается изменяющим; имя и annotations сервера не дают права на безопасный повтор |
| `MCP_HEADERS_JSON` | `{}` | Заголовки исходящих MCP-вызовов |
| `MCP_TIMEOUT` | `30.0` | Таймаут одного вызова; конечное положительное число секунд |
| `MCP_SSE_READ_TIMEOUT` | `300.0` | Таймаут чтения event stream; конечное положительное число секунд |
| `MCP_COLD_START_TIMEOUT_SECONDS` | `300.0` | Общий для всего discovery одного run предел ожидания запуска масштабированных в ноль MCP-серверов при `initialize`/`tools/list`; конечное число `>= 0`, включает сетевые попытки и exponential backoff, при `0` выполняется по одной попытке без повторов |

### Удалённые A2A-агенты

Реестр доверенных агентов и пары имя/значение исходящего auth header настраиваются владельцами в UI; default — `Authorization: Bearer …`. Значения хранятся защищённо и не выдаются модели/процессу/telemetry. Пустой effective registry скрывает `core_agent_send_message`. Runtime использует connection timeout и bounded read-only discovery retry; mutation не повторяется при неизвестном outcome.

Company settings `remote_timeout_seconds` (default86400) и
`remote_poll_interval_seconds` (default300) —положительные bounded integers
с общей owner settings revision. Принятая операция сохраняет их snapshot.
Poll interval задаёт позднюю фазу после780 секунд и ограничивает сверху ранние
интервалы10/30 секунд; возраст сохраняется после restart (LONG-01).
Registry revisions сохраняются immutable с identity actor, изменившего настройку,
и временем записи. Credential encryption использует deployment Fernet key
`PUSH_NOTIFICATION_ENCRYPTION_KEY`; ciphertext не является публичной настройкой.
Development/test in-memory store может иметь ephemeral key. Durable secret
storage/dispatch без persistent key запрещён, даже в development.

`REMOTE_AGENTS`, прежние retry-настройки и `SEND_MESSAGE_API_KEY` являются legacy configuration для явного migration, а не конкурирующим live источником. Сохранённый owner registry становится authoritative после подтверждённого импорта; incoming credential forwarding удалён. Правила import/rollback определены в [Архитектуре](architecture.md).

Operator import получает прежние URL/auth через явно подготовленный version1
JSON-файл `core-agent-db import-remote-agents --file ...`; live startup не читает
его. Требуются `CORE_AGENT_TENANT_ID`, подходящий `DATABASE_MIGRATION_URL` либо
явный operator `DATABASE_URL` и существующий `PUSH_NOTIFICATION_ENCRYPTION_KEY`
для любых credential-bearing entries. Новые defaults или префиксы не добавляются
к header value автоматически. Command summary содержит только число imported
entries; file и values защищаются operator-ом как secrets.

### A2A и streaming

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `A2A_CAPABILITIES` | `streaming,push_notifications,tool_calling,multi_turn` | MAY только сужать реально реализованное. Неизвестное значение завершает startup с `CONFIG_INVALID`. На Agent Card влияют только `streaming` и `push_notifications`; остальные два принимаются для совместимости схемы |
| `A2A_STREAMING_ENABLED` | `true` | Выключение убирает промежуточные кадры и отключает streaming-режим запроса к модели |
| `A2A_STREAMING_BUFFER_SIZE` | `10` | Символов роста до следующего кумулятивного снимка |
| `A2A_MAX_REQUEST_BYTES` | `40000000` | Deployment ceiling encoded HTTP body перед SDK/protobuf; integer 524288–2147483647. Учитывает JSON/base64 целиком, не является decoded company attachment limit; неверное значение — `CONFIG_INVALID` |

### Runtime и лимиты ответа

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `RUNTIME_MAX_LLM_CALLS` | `100` | Hard limit числа model turns |
| `USER_ID` | только explicit development/test | Не является identity fallback для enterprise endpoints; production identity получается только из проверенного Keycloak context |
| `MAX_RESPONSE_SIZE` | `100000000` | Legacy предел transport output; не заменяет единый лимит вложений одного сообщения из owner settings |
| `MAX_CHUNK_SIZE` | `0` | Символов в одном чанке A2A Artifact; `0` означает один чанк |
| `ENTITY_ID` | пусто | Добавляется заголовком `X-Internal-Entity-ID` к исходящим provider и MCP вызовам |

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
| `MEMORY_STORAGE_TYPE` | `in-memory` | Допустимы ровно `in-memory` и `postgres`. Production с включённой памятью MUST использовать `postgres` |
| `MEMORY_POSTGRES_*` | см. [Память агента](memory-service.md) | Отдельный DSN памяти; непустой `MEMORY_POSTGRES_HOST` переопределяет общий пул агента целиком |
| `EMBEDDING_MODEL`, `EMBEDDING_API_BASE`, `EMBEDDING_API_KEY` | пусто | Все три заданные включают слой эмбеддингов; иначе векторный канал поиска деградирует |
| `EMBEDDING_DIMENSION` | `768` | Ожидаемая размерность вектора |
| `MEMORY_SEARCH_LIMIT` | `10` | Сколько записей возвращает `core_memory_search` по умолчанию |

Порядок разрешения URL сессионной базы фиксирован: `SESSION_DATABASE_URL`, затем сборка из `SESSION_POSTGRES_*` при непустом host, затем `TASK_POSTGRES_URL`, затем `DATABASE_URL`.

Dedicated artifact backends/config удаляются после migration входящих файлов. Transport results и snapshots сохраняются по [Файлам и transport artifacts](artifacts.md).

### Telemetry

| Переменная | По умолчанию | Семантика |
|---|---|---|
| `OTEL_ENDPOINT` | пусто | Базовый OTLP endpoint; per-signal `OTEL_EXPORTER_OTLP_*_ENDPOINT` имеют приоритет |
| `OTEL_API_KEY` | пусто | Передаётся заголовком экспортёра |
| `OTEL_SERVICE_NAME` | `core-agent` | Имя сервиса в трассировке |

## Runtime modes

Deployment MUST выбрать один из двух capability-профилей через `CORE_AGENT_RUNTIME_MODE`:

- `with_terminal` — разрешает `core_terminal_exec` и `core_task_start`;
- `without_terminal` — удаляет `core_terminal_exec` и `core_task_start`, но сохраняет task lifecycle (`get`, `list`, `wait`, `cancel`), delegation, MCP, memory и Python.

Значение по умолчанию — `with_terminal`. Неизвестное значение завершает startup с `CONFIG_INVALID`. Runtime mode является верхней границей capabilities: `CORE_AGENT_ALLOWED_BUILTIN_TOOLS`, AgentConfig, Task и delegation contract могут только сузить выбранный профиль. В частности, старый allowlist с terminal tools не может снова включить их в `without_terminal`.

Имя built-in tool в `CORE_AGENT_ALLOWED_BUILTIN_TOOLS`, записанное точками, MUST продолжать называть тот же tool: канонические имена лишились точек, а развёртывания написаны против прежнего написания, и молчаливая потеря capability здесь хуже, чем принятие обоих написаний. Неизвестное имя MUST отклонять startup с перечислением непонятых значений.

`without_terminal` означает отсутствие model-visible terminal capability, а не OS security sandbox: Python-код всё ещё может использовать стандартные `os`, `subprocess` и filesystem APIs внутри обязательной Bubblewrap/egress границы своего чата. Shell, stdio MCP и skill scripts не предоставляются как самостоятельные tools в этом профиле. `core_python_exec` управляется только built-in allowlist.

## Tool filters

Built-in и MCP tools фильтруются после discovery, но до model context:

1. Platform/tenant deny policy;
2. AgentConfig feature switch;
3. AgentConfig server/tool allow/deny;
4. deployment-declared MCP/skills и текущая owner tool policy;
5. delegation allowlist для child.

На каждом уровне deny имеет приоритет. Wildcard разрешён только в namespaced форме вроде `core_task_*` или `core_memory_*`; глобальный `*` SHOULD быть запрещён production policy.

Отключённый tool:

- не показывается модели;
- не может быть вызван по старому имени;
- не наследуется child Task;
- возвращает `CAPABILITY_DISABLED`, если вызов восстановлен из stale model output.

Protocol-internal A2A state transitions, policy checks, audit/redaction и ownership/lifecycle TerminalSession не являются model-callable tools и не отключаются tool filters.

Config validation MUST обнаруживать как минимум:

- `delegation: true` при отключённых background tasks или `core_delegate`;
- `memory: required`, если tool filters не оставляют ни одного `core_memory_*` tool;
- включённую память при `CORE_AGENT_ENVIRONMENT=production` и `MEMORY_STORAGE_TYPE=in-memory`;
- advertised A2A capability без runtime/transport implementation;
- tool allow pattern, полностью перекрытый deny policy;
- skill/MCP requirement, несовместимый с execution/network profile.

`budgets.depth` задаёт максимальную глубину сабагентов относительно main agent с depth `0`. Допустимы только целые значения `0`, `1` и `2`; hard platform maximum `2` не может быть увеличен через deployment environment, AgentConfig или delegation contract.

## MCP policy и roles

MCP descriptor MAY иметь host-validated role, например `repository` или `issue_tracker`. Role не доверяется только потому, что пришла от клиента: AgentConfig/tenant policy сверяет server identity, transport target и optional integrity metadata.

Для каждого server можно настроить:

- allowed transports/targets;
- required/optional behavior;
- capability types tools/resources/prompts/sampling/elicitation;
- tool allow/deny patterns;
- secret refs;
- network/execution profile;
- trusted capability policy profile.

Роль `memory` не назначается ни одному серверу: память является подсистемой Core Agent, а не MCP-интеграцией.

## Skills policy

AgentConfig задаёт allowed sources, names, versions, permissions и default deny/allow. MCP и skills принадлежат deployment; RunRequest их не передаёт. Child получает только явно делегированный subset.

Для local filesystem packages `SKILLS_ROOT` задаёт корень каталогов, а
`CORE_AGENT_ALLOWED_SKILLS` — точный deployment allowlist имён. Пустой allowlist
отключает discovery независимо от содержимого root. Runtime валидирует каждый
пакет и закрепляет его digests до первого model call; установка или обновление
пакетов во время Task запрещены.

## EffectiveConfig snapshot

Перед A2A Task runtime вычисляет immutable EffectiveConfig:

```text
EffectiveConfig = PlatformConfig ∩ tenant policy ∩ AgentConfig ∩ Task capabilities
```

Snapshot содержит versions/digests profile prompt, kernel policy packs, model route, tool schemas, MCP/skill locks, budgets, context thresholds и execution/OTel profiles.

- Unknown config fields MUST отклоняться.
- Conflict MUST возвращать path и безопасную причину.
- Infrastructure/model/budget snapshot меняется только для новой Task. Owner tool policy и guardrails exceptions применяются к последующим calls активных Tasks внутри immutable ceiling; они не прерывают уже dispatched call. Новое разрешение не восстанавливает закрытое ожидание и не автоматически одобряет ожидающий HITL.
- Agent Card генерируется из Effective AgentConfig и не рекламирует disabled capability.
- Каждый authenticated GET Agent Card также учитывает текущую owner tool policy
  компании из проверенной identity. Запрещённые tools не рекламируются на обоих
  A2A входах и обоих well-known путях; `allow` и `require_hitl` остаются видимыми
  внутри platform ceiling. Изменение policy не требует перезапуска приложения.
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

## Настройки владельцев

### UI-02A. Профиль, модель и MCP

`GET/PUT /api/agent-settings` доступны только verified company owners. Settings
имеют company scope и одну CAS `revision`; PUT содержит `expected_revision` и
только изменяемые секции. Concurrent stale write возвращает `SETTINGS_CONFLICT`
без частичного сохранения. `profile_prompt` и `model_id` nullable: `null`
наследует deployment default, пустой profile явно очищает профиль. Read
возвращает effective значения и признаки наследования. Профиль ограничен
65 536 UTF-8 bytes и не может менять safety, host/kernel или capability policy.

`GET /api/agent-settings/models` выполняет только bounded read-only GET `models`
на фиксированном deployment provider connection, без model inference. Response
содержит отсортированные уникальные безопасные model IDs, максимум 1000 IDs,
и current selection. Transport имеет предел 10 секунд и 1 MiB; credentials,
provider URL и raw provider errors не выдаются. Model ID ограничен 256 ASCII
characters `[A-Za-z0-9_.:/-]`. Изменение selection допускает только ID из текущего
успешного списка; недоступность discovery возвращает `MODEL_DISCOVERY_UNAVAILABLE`
и сохраняет прежнее значение. Provider route, credentials, context window,
sampling и guardrails adapter остаются trusted deployment configuration.
Основные model turns, semantic compaction и настроенный memory NER используют
model ID admitted run; embedding provider и отдельный guardrails adapter
сохраняют свои deployment connections. Token estimate остаётся локальным.

MCP section содержит максимум 32 уникальных connections: стабильное имя
`[A-Za-z0-9_-]{1,64}`, HTTPS URL до 4096 bytes (loopback HTTP разрешён), enabled
и optional custom auth header. URL не содержит credentials, query или fragment.
Transport-managed и hop-by-hop headers запрещены. Header value принимается
только на запись: отсутствие поля сохраняет secret при неизменных URL/header,
`null` удаляет его, непустое значение заменяет. Изменение URL/header требует
явного нового secret либо удаления прежнего; deployment headers никогда не
наследуются новой owner connection. Secret ограничен 16 KiB Latin-1 без control
characters. Read содержит только header name и `has_header_value`; secret
защищён Fernet envelope с tenant/revision/name/header binding и persistent
`PUSH_NOTIFICATION_ENCRYPTION_KEY` в PostgreSQL. Ошибки, model context, audit
и telemetry не содержат credentials. Deployment MCP defaults действуют до
явного изменения MCP section владельцем; остальные секции их не заменяют.
Read показывает deployment URL без userinfo/query/fragment. Сохранение прежнего
имени и этого projected URL с пустым auth header сохраняет trusted deployment
transport/headers при включении и отключении; это ссылка на существующее
подключение. Новый URL не получает credentials другого подключения.

Owner-managed MCP connection является trusted company configuration: её
discovered tools доступны внутри platform feature ceiling, с default HITL
и существующей per-tool policy. Discovery content не расширяет child allowlist.
Disable/delete влияет на новые roots; прежние immutable revisions и credentials
сохраняются для принятых Tasks. Новый owner/external/cron root атомарно закрепляет
company revision, effective profile, model ID и MCP declarations до network.
Follow-up, HITL/guardrail/time/task wait, delegated children и recovery сохраняют
этот snapshot. Live tool-policy deny продолжает проверяться перед dispatch.
Legacy snapshots без company revision читаются прежним путём без перезаписи.
Schema migration создаёт current pointer и immutable version1 revision rows;
serving role не изменяет/удаляет revisions. Откат после новых settings records
требует совместимого reader либо согласованного восстановления БД.

### UI-02. Настройки

В UI владельцы управляют политикой каждого инструмента, исключением инструмента
из проверки guardrails, доверенными внешними агентами и расписаниями cron.
Согласованные таймауты ожиданий, интервал опроса внешних задач и лимит вложений
настраиваются в UI. Уже сохранённые абсолютные сроки ожиданий не сбрасываются
при изменении настройки. Корень папок чатов задаётся через env;
подключения к инфраструктуре относятся к конфигурации развёртывания.


| Настройка UI | Default | Семантика |
|---|---|---|
| Tool policy | HITL для нового tool | `allow`, `require_hitl`, `deny` внутри platform ceiling |
| Guardrails exception | выключена | Только owner; отдельно для arguments/results конкретного tool |
| HITL timeout | 24 часа | Абсолютный срок конкретного запроса |
| Owner-answer timeout | 24 часа | Отдельный срок вопроса владельцу |
| Guardrails decision timeout | 24 часа | Отдельный срок решения по материалу |
| Remote operation timeout | 24 часа | Окончательный срок операции, повторный wait не продлевает |
| Remote polling interval | 10 с первые 3 мин; 30 с следующие 10 мин; затем 5 мин | GetTask без model/tool budget; закреплённый configurable интервал ограничивает ранние фазы сверху |
| Общий лимит вложений сообщения | 25 000 000 байт | Один предел inbound/outbound UI/A2A; 1 МБ = 1 000 000 байт |
| Cron timezone | `Europe/Moscow` | Сохранённый IANA identifier на расписание |

Изменение timeout не переписывает уже сохранённые deadlines. `CHAT_WORKSPACE_ROOT` задаётся deployment env и требует постоянный POSIX том; `LOCAL_WORKSPACE_ROOT` остаётся ephemeral. Keycloak, secret storage, model/guardrails model connections и sandbox/egress profile принадлежат trusted deployment. Guardrails модель настраивается отдельно, default использует подключённую модель агента в отдельном контексте без tools.
