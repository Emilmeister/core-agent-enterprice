# Core Agent: инструкция для разработчиков-агентов

## Область действия и источник истины

Этот файл действует на весь репозиторий. Он является навигатором и рабочим
контрактом для разработчиков, но не заменяет нормативную спецификацию.

- `spec/` задаёт ожидаемое поведение продукта.
- `spec/releases/v1.md` задаёт текущий поставляемый срез target spec.
- `spec/implementation-status.md` связывает требования с автоматическими
  доказательствами.
- Код, тесты, README, schemas и examples реализуют спецификацию, но не
  переопределяют её.
- При конфликте действует приоритет из `spec/README.md`: безопасность,
  публичный контракт, acceptance, затем остальные документы.

## Обязательные правила перед любой работой

1. Прочитать этот файл, `spec/README.md` и только относящиеся к задаче документы
   по карте ниже.
2. Проверить `git status --short`. Считать все существующие изменения и
   untracked-файлы пользовательскими, пока не доказано обратное.
3. Для любой задачи, изменяющей репозиторий, включая код, архитектуру, tests,
   Markdown и repository guidance, всегда использовать skill `ponytail`.
4. Для Python dependency management, запуска, тестов, lint и build всегда
   использовать `uv`.
5. До редактирования проследить реальный end-to-end flow и всех callers
   изменяемого shared-кода. Исправлять первопричину в общей точке.
6. Проверить, разрешено ли менять замороженные spec/tests.

## Замороженные spec и tests

Любое создание, изменение, удаление, переименование или перемещение внутри
`tests/**` и `spec/**/*.md` запрещено без новой явной директивы пользователя.
`tests/test_spec_lock.py` фиксирует набор и точные bytes всех spec-файлов.

- Без свежего явного разрешения не менять tests, spec-файлы и frozen hashes.
- Запрос на анализ, диагностику или создание документации сам по себе не даёт
  такого разрешения.
- Постоянное требование поддерживать `AGENTS.md` актуальным разрешает менять
  только этот файл, когда его факты устарели; оно не размораживает spec/tests.
- Не обходить запрет созданием дублирующего теста с тем же назначением.
- Если исправление только возвращает код к уже зафиксированной спецификации,
  менять spec не нужно. Если необходимое observable behavior расходится со
  spec или требует изменения существующего теста, остановиться и запросить
  новую директиву до этих правок.
- При полученном разрешении менять spec/tests осознанно и обновлять hash lock,
  а не ослаблять проверку ради зелёного CI.

## Ponytail обязателен всегда

Перед изменениями прочитать актуальный `SKILL.md` skill-а `ponytail` и применить
его ladder:

1. Проверить, действительно ли изменение нужно.
2. Найти и переиспользовать существующее решение в репозитории.
3. Предпочесть standard library.
4. Предпочесть native platform capability.
5. Использовать уже установленную dependency.
6. Только затем добавить минимальный код, который полностью решает задачу.

Не добавлять speculative abstractions, factory с одной реализацией, dependency
ради нескольких строк или конфигурацию «на будущее». Но Ponytail никогда не
разрешает упрощать validation на trust boundary, security, durability,
tenant isolation, recovery, error handling, accessibility или явно заданные
acceptance guarantees.

## Spec-driven development

Любая новая возможность, изменение observable semantics или product contract
идёт в следующем порядке:

1. Сформулировать проблему и наблюдаемый target result.
2. Получить разрешение на frozen spec/tests, если требуются их изменения.
3. Обновить основной target-документ в `spec/`.
4. Зафиксировать выбранную семантику и только реально значимый trade-off.
5. Добавить проверяемый criterion в `spec/acceptance.md`.
6. Обновить `spec/releases/v1.md`, если меняется release scope.
7. Реализовать минимальное изменение и regression proof.
8. Обновить `spec/implementation-status.md` только когда automated proof входит
   в обычный CI suite и этот gate проходит. Manual happy-path demo не является
   статусом `implemented`.
9. Для public API или persisted state определить compatibility, versioning и
   migration behavior.
10. Выполнить targeted проверки, затем все применимые release gates.

Security fix может потребовать начать с кода, но normative spec и regression
criterion должны войти в тот же завершённый change. Throwaway prototype не
определяет публичную семантику задним числом.

Один логический product change должен оставаться одним reviewable commit.
Отдельный ADR нужен только для трудно обратимого решения, затрагивающего
несколько подсистем или требующего сохранить отклонённые альтернативы; см.
`spec/development-process.md`.

## Поддержание AGENTS.md

Каждое изменение репозитория включает обязательную проверку актуальности этого
файла.

- Если изменились команды, layout, entrypoints, runtime modes, tools,
  architecture boundaries, configuration, persistence, security invariants или
  workflow разработки, обновить `AGENTS.md` в том же commit.
- Если эти факты не изменились, не создавать бессодержательный diff только ради
  timestamp или отметки «проверено».
- Не превращать файл в changelog и не записывать сюда commit hashes, текущий
  test count, frozen spec hashes или временный статус.
- Предпочитать стабильные правила и ссылки на authoritative spec.

## Что находится в репозитории

- `core_agent/` — policy-enforced agent runtime, встроенная память и A2A
  transport.
- `spec/` — target specification, acceptance и release profiles.
- `tests/` — frozen acceptance, unit, integration, PostgreSQL, A2A и E2E suite.
- `.github/workflows/ci.yml` — канонический CI-порядок.
- `docker-compose.yml` — локальный PostgreSQL, migration job, agent и Phoenix.
- `.env.example` — поддерживаемый шаблон локальной конфигурации; `.env` никогда
  не коммитится.

Package entrypoints из `pyproject.toml`:

- `core-agent` → `core_agent.app:main`;
- `core-agent-db` → `core_agent.database:main`.

## Карта ключевых implementation-файлов

| Файл | Ответственность |
|---|---|
| `core_agent/app.py` | Composition root: environment config, stores, tool registry, kernel, A2A app, health и Uvicorn |
| `core_agent/runtime.py` | Agent loop, workflow continuation, recovery, tool handlers и delegation |
| `core_agent/config.py` | RunRequest, Platform/Agent/EffectiveConfig и capability intersection |
| `core_agent/a2a.py` | Внутренние A2A contract types и Task representation |
| `core_agent/a2a_sdk.py` | Official A2A SDK HTTP+JSON binding и request handler |
| `core_agent/model.py` | OpenAI-compatible/Anthropic adapters, streaming и tool wire payload |
| `core_agent/kernel.py` | Protected kernel, profile и skill instruction layers |
| `core_agent/context.py` | Context budget, compaction и structured summary |
| `core_agent/tools.py` | Tool schemas, validation, policy и dispatch |
| `core_agent/execution.py` | PTY/process groups, owned workspaces, snapshots и limits |
| `core_agent/python_exec.py` | Bounded Python process и `tools.call(...)` broker |
| `core_agent/tasks.py` | Test scheduler, mailbox и delegation contracts |
| `core_agent/postgres_tasks.py` | Durable PostgreSQL scheduler и mailbox |
| `core_agent/workflow.py` | Workflow stores, transitions и durable outbox |
| `core_agent/database.py` | PostgreSQL schema, migrations, pool и stores |
| `core_agent/artifacts.py`, `core_agent/audit.py` | Tenant-scoped transport artifacts и append-only audit adapters |
| `core_agent/artifact_service.py` | Named/scoped/versioned artifact model и in-memory, S3, MongoDB backends |
| `core_agent/remote_agents.py` | Remote A2A agent registry и synchronous JSON-RPC/SSE client |
| `core_agent/streaming.py` | Stream chunk merge, snapshot buffer и ADK metadata keys |
| `core_agent/durability.py`, `core_agent/lifecycle.py` | Events, checkpoints, leases, recovery и retention |
| `core_agent/mcp.py` | MCP discovery/calls, canonical tool naming и Streamable HTTP connector |
| `core_agent/security.py` | Redaction, safe paths, retry и tenant helpers |
| `core_agent/skills.py` | Skill resolution и integrity metadata |
| `core_agent/observability.py` | OpenTelemetry/OpenInference spans и exporters |
| `core_agent/push.py` | Durable encrypted A2A push delivery |
| `core_agent/memory.py` | Markdown revisions, hybrid retrieval, NER, graph index и `core_memory_*` tools |
| `core_agent/memory_store.py` | In-memory и PostgreSQL backends memory corpus |
| `core_agent/memory_providers.py` | Embedding provider и извлечение сущностей моделью агента |

## Progressive disclosure: что читать

Всегда начинать с `spec/README.md`, затем выбирать только нужное:

- product boundaries — `spec/product.md`;
- architecture и durable state — `spec/architecture.md`;
- config и runtime modes — `spec/agent-configuration.md`;
- A2A/public input — `spec/a2a-protocol.md`, `spec/public-contract.md`;
- agent loop и recovery — `spec/runtime.md`;
- protected instructions — `spec/kernel-instructions.md`;
- tools — `spec/tools.md`;
- artifact storage — `spec/artifacts.md`;
- background/subagents и remote A2A agents — `spec/tasks-and-delegation.md`;
- terminal/workspaces/Python — `spec/execution-environment.md`;
- context compaction — `spec/context.md`;
- память агента — `spec/memory-service.md`;
- skills — `spec/skills.md`;
- observability — `spec/observability.md`;
- security/reliability — `spec/security-and-reliability.md`;
- acceptance/status/release — `spec/acceptance.md`,
  `spec/implementation-status.md`, `spec/releases/v1.md`;
- change process — `spec/development-process.md`.

Target spec может описывать больше текущего runtime. Не объявлять capability
реализованной только потому, что она присутствует в target-документе.

## Product и runtime invariants

### Публичный контракт и инструкции

- Run input содержит ровно `prompt`; MCP-серверы и skills задаются
  конфигурацией развёртывания и не добавляются в RunRequest.
- Tenant, identity, auth, trace context, model route, policy и budgets приходят
  через authenticated transport/platform config, а не добавляются в RunRequest.
- A2A является внешним lifecycle. Не создавать параллельную несовместимую task
  state machine в transport или adapter.
- Non-terminal A2A Task принимает follow-up по тому же `taskId`. Adapter durable
  пишет его в workflow inbox; runtime доставляет отдельным user turn только на
  safe boundary и атомарно не завершает Task, пока есть принятый unread input.
- Follow-up не прерывает текущий model/tool call, не меняет EffectiveConfig или
  budgets и дедуплицируется по `(task_id, message_id)`. После terminal state
  продолжение создаёт новую Task в том же context.
- Перед `FAILED`, `CANCELLED`, `REJECTED` или `ABORTED` все уже принятые
  follow-up атомарно переносятся в transcript как
  `unprocessed_due_to_failure|cancel` вместе с terminal transition. Они не
  остаются unread и не доставляются модели после recovery.
- Terminal A2A Artifact различает полное и budget-exhausted завершение через
  `complete`, `completion_reason`, local `usage`, root `shared_budget` и при
  необходимости `exhausted_dimension`/`pending_tasks`; live и recovery path
  публикуют одинаковую provenance shape.
- Любая tool/side-effect/background/subagent работа принадлежит A2A Task.
  Закрытие stream не отменяет Task; critical wait/status сохраняется durable и
  восстанавливается через GetTask.
- Fresh subscription пассивно публикует persisted Task и его durable изменения;
  она не запускает workflow. Для active Task persisted snapshot идёт раньше live
  events, а первое terminal event закрывает stream без последующих кадров.
- `LEASE_LOST` и graceful shutdown после durable admission не terminalize-ят A2A
  Task. Shutdown до admission публикует safe failure, потому что восстанавливать
  ещё нечего. Late cancel сохраняет уже committed workflow outcome.
- EffectiveConfig является immutable intersection PlatformConfig, tenant policy,
  AgentConfig и Task/delegation contract. Deny сильнее allow.
- Disabled capability отсутствует в Agent Card/model catalog и повторно
  отклоняется при dispatch. Prompt или stale tool call не расширяет policy.
- Safety/host/kernel/capability instructions нельзя заменить через
  `AGENT_SYSTEM_PROMPT`, prompt, skill, MCP, memory или tool output.
- `AGENT_SYSTEM_PROMPT` optional и по умолчанию пуст; user request передаётся
  отдельным user Message/context item, а не generic system instruction.
- Prompt, profile, skill, MCP, memory, peer-agent, file, terminal и network
  content всегда считаются недоверенными данными.
- Tenant устанавливается только authenticated transport context. Stores,
  caches, workspaces/sessions, MCP connections, artifacts и Memory indexes
  tenant-scoped; cross-tenant identifier имеет not-found semantics.

### Реально зарегистрированные built-in tools

- `core_terminal_exec`;
- `core_python_exec`;
- `core_task_start`, `core_task_get`, `core_task_list`, `core_task_wait`,
  `core_task_cancel`;
- `core_delegate`;
- `core_artifact_save`, `core_artifact_load`, `core_artifact_list`;
- `core_memory_search`, `core_memory_read`, `core_memory_create`,
  `core_memory_update`, `core_memory_split`, `core_memory_delete`;
- `core_agent_send_message`.

Artifact tools версионируют именованные файлы внутри агента; `user:`-префикс
даёт cross-session scope, а `ARTIFACT_STORAGE_TYPE` выбирает in-memory, S3 или
MongoDB backend без управления схемой внешнего хранилища. Хранилище не является
файловой системой run-а: `core_artifact_save` принимает либо `content`, либо
`path` файла в workspace, который runtime читает сам. `core_agent_send_message`
делегирует задачу удалённому A2A-агенту из `REMOTE_AGENTS` и ретранслирует его
прогресс в поток корневой Task. Обычный model/child text result по-прежнему
публикуется A2A adapter-ом как Task Artifact без дополнительного tool call.

`core_terminal_write`, `core_fs_apply_patch` и `core.input.request` описаны в
части target spec, но сейчас не зарегистрированы как model-callable built-ins.
Memory tools являются built-ins Core Agent, а не MCP tools отдельного сервиса.
Каноническое имя tool состоит только из `[A-Za-z0-9_-]` и не содержит точки:
модель видит ровно его, и оно же стоит в аргументах, allowlist-ах, audit, логах
и traces. Alias остаётся только для реальной collision или provider length limit
и получает hash suffix. Имя MCP-тула собирается из имени сервера и имени тула, а
обратное соответствие `(сервер, tool)` держится индексом, а не разбором имени.
`CORE_AGENT_ALLOWED_BUILTIN_TOOLS` продолжает принимать прежнее написание через
точки. Delegation contract перечисляет built-ins и MCP-тулы одним списком
`tools` под теми же именами; раскладку на серверы делает runtime.

Подсистема памяти публикует модели ровно шесть tools выше; `move`, `history`,
`index_status` и `entity_resolve` остаются внутренними методами. `MCP_ALLOWED_SERVERS`
и `MCP_ALLOWED_TOOLS` пусты по умолчанию: MCP-сервер и его tools требуют явной
platform configuration, а зарезервированного сервера `memory` не существует.
`MCP_READ_ONLY_TOOLS` также пуст по умолчанию: неизвестный MCP-tool считается
mutating независимо от имени и server annotations. Только доверенная запись
голого имени или `server.tool` разрешает обработать неизвестный transport
outcome как ошибку read-only операции; scoped-форма не действует на другие
серверы.
При каждом новом run Streamable HTTP discovery повторяет только временные
ошибки до одного общего для всех MCP `MCP_COLD_START_TIMEOUT_SECONDS` (по
умолчанию 300 секунд). Workflow и абсолютный срок сохраняются до сети, поэтому
follow-up, cancel и recovery видят тот же Task и не начинают срок заново. Каждый
run получает отдельные MCP session и negotiated version; перед initialize они
очищаются. Ожидание не расходует model/tool budget. Permanent auth/protocol
ошибки и неоднозначный mutating `tools/call` не повторяются; cancel прерывает
ожидание.

### Runtime modes и execution

- `with_terminal` может публиковать `core_terminal_exec` и `core_task_start`.
- `without_terminal` удаляет эти два tool, сохраняя task lifecycle, delegation,
  MCP и memory.
- `core_python_exec` доступен в обоих режимах, если не удалён
  `CORE_AGENT_ALLOWED_BUILTIN_TOOLS`.
- `CORE_AGENT_ALLOWED_BUILTIN_TOOLS` только сужает выбранный mode ceiling.
- `CORE_AGENT_BUDGET_CANCEL_GRACE_SECONDS` задаёт положительное bounded ожидание
  подтверждения cancel owned Tasks перед возвратом budget-partial результата.
- `without_terminal` означает отсутствие model-visible terminal tool, а не OS
  sandbox: Python может использовать `os`, `subprocess` и filesystem APIs.
- `core_terminal_exec` принимает `argv` без implicit shell. Pipes, redirects и
  `&&` требуют явного `['sh', '-lc', '...']` и отдельной policy оценки.
- Образ v1 содержит основной CLI-набор для текста, файлов, структурированных
  данных, архивов, сети и PDF, включая Mike Farah `yq` и команду `fd`. Краткий
  model-facing перечень не является allowlist; non-root agent может добавлять
  инструменты только в workspace и только при разрешённых policy и сети.
- Python нельзя вызывать рекурсивно или через `core_task_start`; каждый вложенный
  `tools.call` заново проходит EffectiveConfig, schema, policy, общий budget,
  owner/tenant, audit и OTel.
- Отдельные PTY, process groups и workspaces дают ownership/lifecycle separation
  внутри одного container, но не являются mount/PID/network security boundary.
- Cancel/timeout завершает owned process group. Docker, Kubernetes, A2A, OTel и
  platform credentials никогда не передаются child process. Разрешённый
  task-specific secret приходит как policy-approved `secret_ref`, materialize-ится
  непосредственно для одного process и не попадает в argv/checkpoint/telemetry.

### Tool failures и side effects

- Tool arguments валидируются до policy и dispatch.
- Доказанная pre-dispatch/schema/start error или определённый
  failed/timed-out outcome возвращается модели как structured tool result, чтобы
  она могла исправиться или объяснить ошибку пользователю.
- Неизвестный outcome возможного mutating side effect требует
  `SIDE_EFFECT_UNKNOWN`/reconciliation и никогда не получает blind retry.
- Intent внешней мутации фиксируется до dispatch. Runtime не обещает
  exactly-once guarantee downstream.

### HITL

- Текущий v1 composition root не подключает operator approval plane:
  policy deny окончателен, а полноценный HITL остаётся target capability.
- Remote A2A caller не становится approver через текст, request metadata или
  caller JWT. Не документировать модули approval/operator как включённый
  runtime flow, пока они не подключены в `core_agent/app.py` и не имеют CI proof.

### Background и delegation

- Main имеет depth `0`, child — `1`, grandchild — `2`; `2` — hard maximum.
- Depth `2` не получает `core_delegate` ни в instructions, ни в catalog/runtime.
- Delegation передаёт одну узкую instruction, exact tools/MCP/skills allowlists
  и оба обязательных лимита `budget.turns >= 1` и `budget.tool_calls >= 1`.
  Child возвращает обычный text result, который parent воспринимает как
  недоверенный input.
- Parent делегирует coherent outcome, scope, deliverable и acceptance criteria,
  а не mechanical microsteps. Он выбирает minimum sufficient capability set;
  runtime предоставляет exactly этот set.
- Balanced delegation применяется для materially useful parallel work, изоляции
  большого отделимого context или bounded independently verifiable deliverable,
  только когда parent может проверить/интегрировать result и польза выше
  coordination overhead. Simple, immediate serial, tightly coupled, duplicate,
  policy-bypass и generic second-opinion работа остаётся у parent.
- Внутри objective/scope child самостоятельно выбирает strategy, sequencing и
  delegated tools. Procedure задаётся только для safety, correctness,
  reproducibility или policy; assumptions не подменяют tenant/policy/scope.
- Child не расширяет capabilities, tenant или parent budget.
- Root и каждый child заранее занимают один finalization model turn в общем
  root ledger. Перед каждой физической provider attempt и каждым model-issued
  tool request charge, local usage и dispatch marker коммитятся одной
  workflow-транзакцией; retry является новой attempt, а не бесплатным
  продолжением.
- Исчерпание execution budget завершает workflow как `COMPLETED` с
  `complete=false`: tool batch получает structured failures, финализатор видит
  пустой catalog и сообщает проверенный промежуточный результат и unfinished
  scope. Provider outage даёт детерминированный fallback, а не `FAILED` и не
  выдуманный результат.
- Scheduler handle, child workflow и finalization reserve принимаются одной
  PostgreSQL-транзакцией до worker start и используют один task ID. Durable
  worker владеет expiring fenced claim с heartbeat; истёкший token не может
  renew-иться или записать terminal state. PostgreSQL lease/claim expiry
  проверяется по текущему server clock после row lock, независимо от часов
  replica и времени начала statement. Workflow recovery пропускает live lease и
  само получает lease перед reconciliation; scheduler принимает recovery/cancel
  решение по provenance atomic claim, а late cancel не стирает reconciliation
  error. Workflow transition делает fenced `core_runs` update последней записью
  транзакции; expiry во время budget/audit/outbox lock wait откатывает transition
  и его budget charge целиком.
- Joined `core_delegate` по умолчанию требует использовать child result и не
  повторять ту же работу. `background: true` разрешает main продолжать только
  независимую работу до notification/wait.
- `core_task_wait` является passive wait; busy polling запрещён.
- Parent cancel рекурсивно отменяет owned children. Required pending child
  блокирует успешное завершение parent; нужный финальному ответу result должен
  быть joined/waited.
- При budget exhaustion cancel ждётся только настроенный grace period.
  Неподтверждённая Task остаётся durable/cancel-requested, перечисляется в
  `pending_tasks`, а её поздний result/notification сохраняется; owned terminal
  process group получает teardown callback.
- Workflow в `EXECUTING` не resume-ит persisted tool call: mutating exception,
  recovery или cancel сохраняют `SIDE_EFFECT_UNKNOWN`, а scheduler не маскирует
  его состоянием `canceled`.
- Child failure возвращается parent как structured result и сам по себе не
  обязан завершать parent Task.
- Shared memory существует только при явной делегации memory tools: child
  наследует ту же тройку scope, а без делегированного tool работает без памяти.
  Scratchpad/context не является общей памятью.

### Persistence и storage

- Production использует PostgreSQL и fail closed без настроенного DSN, доступной
  DB и совпадающей schema version. Нет SQLite/in-memory fallback.
- Tasks, workflow events/checkpoints, inbound inbox, outbox и audit
  tenant/owner-scoped и сохраняются в PostgreSQL согласованно.
- После restart ambiguous dispatched side effect переходит в reconciliation, а
  не replay. Shared storage lock не заменяет lease: у stateful run один active
  owner.
- Root recovery coordinator сканирует durable безопасные состояния автоматически;
  root-фильтр применяется до batch limit. Проигравший claim завершается на первом
  `LEASE_LOST` и ждёт следующего scan вместо локального polling. Graceful shutdown
  оставляет admitted workflow для следующего владельца.
- Recovery reconciliation одной транзакцией обновляет A2A Task и ставит push в
  idempotent ledger; recovered failure содержит только stable safe error code.
- Serving process не выполняет production auto-migration; migration job имеет
  отдельный `DATABASE_MIGRATION_URL`.
- `DURABLE_STORAGE_ROOT` предназначен для immutable durable blobs, snapshots и
  artifacts, обычно на S3-backed mount.
- `LOCAL_WORKSPACE_ROOT` является отдельным ephemeral filesystem для активных
  процессов и не находится внутри durable mount.
- Memory corpus живёт в backend из `MEMORY_STORAGE_TYPE` (процесс или общая БД
  агента) и не имеет собственного filesystem root; не смешивать storage domains.
- Test/in-memory adapters разрешены только в явно выбранном
  development/test profile.

### Память агента

- Память является подсистемой Core Agent, а не отдельным сервисом. Отдельного
  memory-процесса, memory-контейнера и MCP-роли `memory` не существует.
- `CORE_AGENT_MEMORY` задаёт feature mode `optional|required|disabled`, а
  `MEMORY_STORAGE_TYPE` выбирает `in-memory` или `postgres`. При `disabled` ни
  один `core_memory_*` tool не попадает в catalog и backend не создаётся; при
  `required` неработоспособный backend является ошибкой старта. Production с
  включённой памятью и `in-memory` fail closed.
- Модель не имеет ни filesystem, ни SQL доступа к corpus: любая mutation идёт
  через `core_memory_*`. Namespace выводится runtime-ом из scope текущего run,
  модель передаёт только `user|session`; secrets и private reasoning в memory
  запрещены.
- Normative target из `spec/memory-service.md`: Markdown является canonical
  source, а BM25/vector/NER/graph — rebuildable derived state.
- Текущая реализация считает BM25 во время search, embedding изменённого
  документа вычисляет один раз при публикации и хранит вместе с документом, а
  NER/entities/links пересчитывает до commit. Не предполагать иной
  storage/index lifecycle без проверки кода и разрешённого spec change.
- Body Markdown-файла ограничен 200 строками. 201+ отклоняется до publication
  без truncation, revision change или automatic split.
- Ошибка memory tool возвращается модели как обычный failed tool result и не
  завершает run: на `MEMORY_FILE_TOO_LARGE` модель отвечает явным
  `core_memory_split`.
- Agent instructions требуют search перед create/update и рекомендуют update
  существующего topic вместо duplicate; подсистема этого не проверяет. Split
  выполняется только отдельным явным call.
- Mutation атомарно публикует committed Markdown snapshot, derived
  NER/entities/links и repository revision; stale edges удаляются,
  last-write-wins запрещён.
- Search не видит staging revision; index failure сохраняет предыдущую
  committed revision.
- Отсутствующий embedding или NER provider только помечает соответствующий
  канал degraded и не роняет tool call; production настраивает настоящие
  providers, а встроенный regex-экстрактор остаётся development/test.

### Observability и secrets

- Durable audit является product record и не заменяется OTel.
- `THINKING_LEVEL` optional и маппится в native OpenAI-compatible/Anthropic request; пустое значение сохраняет provider default и никогда не управляется через RunRequest/prompt.
- Provider-visible reasoning остаётся operator-only content: stdout требует `CORE_AGENT_LOG_CONTENT=true`, Phoenix — `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`; hidden/opaque thinking не экспортируется.
- Incoming A2A call не создаёт отдельный transport/submission trace: agent processing сразу начинается `core_agent.task.execute`, продолжая валидный incoming W3C parent или становясь local root.
- Child-agent span продолжает parent trace; независимая background/durable
  работа использует новый trace со Span Link. Trace context не является auth.
- Логи используют canonical tool names, correlation IDs и bounded one-line JSON.
- Provider-hidden/private/opaque reasoning, credentials, secret values, auth
  headers и raw provider errors никогда не попадают в public errors, logs,
  spans или artifacts; provider-visible reasoning подчиняется explicit
  operator-only content gates выше.
- Content capture включается только явной privileged policy с redaction,
  access, sampling и retention.
- Не использовать prompt, paths, commands, entities, tenant/user/task IDs как
  metric labels.
- Production push configuration и tokens шифруются отдельным
  `PUSH_NOTIFICATION_ENCRYPTION_KEY`. Delivery использует durable at-least-once
  ledger и stable ID. Target проверяется при registration и каждом send: только
  HTTPS:443, без redirect/userinfo, со всеми DNS answers public/global.

## Code и worktree conventions

- Python `>=3.12`; тесты используют standard-library `unittest`.
- Использовать `rg`/`rg --files` для поиска и `apply_patch` для ручных правок.
- Не добавлять bare `pip`, Poetry или альтернативный virtualenv workflow.
- Не менять dependency без необходимого `pyproject.toml` и `uv.lock` diff.
- Не скрывать ошибку широким `except`, fallback или ослаблением fail-closed
  behavior.
- Публичные ошибки имеют стабильный safe code; stack trace и provider details
  остаются только в защищённом operator channel.
- Сохранять unrelated user changes; не выполнять destructive Git commands.
- При использовании development subagents давать им узкую независимую задачу.
  Избегать overlapping writes: один владелец файла, main интегрирует и проверяет.
- Не коммитить `.env`, credentials, tokens, generated state, `.idea/` и другие
  локальные IDE-файлы.

## Канонические команды

Setup:

```bash
uv sync --frozen
```

Dependency changes делаются через `uv add`/`uv remove`; `uv.lock` вручную не
редактируется. После изменения lockfile снова выполнить `uv sync --frozen`.

Lint:

```bash
uv run ruff check core_agent tests
```

Targeted test, пример:

```bash
uv run python -m unittest tests.test_end_to_end -v
```

Полный suite:

```bash
uv run python -m unittest discover -s tests -v
```

Build:

```bash
uv build --no-sources
docker build -t core-agent:local .
```

PostgreSQL gates требуют реальные URLs; skipped database tests не являются
production proof:

```bash
DATABASE_MIGRATION_URL=postgresql://... uv run core-agent-db migrate
TEST_DATABASE_URL=postgresql://... uv run python -m unittest discover -s tests -v
```

Local stack:

```bash
cp .env.example .env
# Заполнить локальные database/model credentials; файл не коммитить.
docker compose config --quiet
docker compose up --build -d
docker compose logs -f agent
```

Не выполнять `docker compose down -v`, если пользователь явно не разрешил
удалить volumes.

## Какие проверки обязательны

- Documentation-only: проверить links/examples и `git diff --check`; если
  затронута spec — также
  `uv run python -m unittest tests.test_spec_quality tests.test_spec_lock -v`.
- Runtime/config/tool change: targeted regression, Ruff и полный unittest suite.
- Persistence, HITL, race или recovery: PostgreSQL suite с `TEST_DATABASE_URL`;
  отсутствие DB и skipped tests явно сообщить.
- A2A change: protocol/config tests и соответствующий HTTP E2E.
- Memory change: `tests.test_memory_service` и PostgreSQL suite с
  `TEST_DATABASE_URL`, так как backend памяти является database concern.
- Docker/Compose/startup/permissions: повторить релевантные image/Compose smoke
  из `.github/workflows/ci.yml`.
- Dependency/build change: `uv sync --frozen`, Ruff, suite, `uv build --no-sources`
  и image smoke.

При failed или skipped обязательном gate не заявлять полную production-ready
верификацию.

## Git и commit discipline

- До и после работы проверять `git status --short` и финальный diff.
- Stage только относящиеся к задаче файлы; никогда не использовать `git add .` в
  dirty worktree.
- Один commit — одно логическое product decision.
- Spec коммитится до implementation либо вместе с первым implementation slice.
- Использовать сложившиеся prefixes: `feat:`, `fix:`, `spec:`, `test:`, `ci:`,
  `build:`.
- Message описывает доставленное поведение, а не факт редактирования Markdown.
- Не amend/rebase/reset и не переписывать историю без прямого запроса.

## Definition of done

Перед завершением изменения убедиться:

1. Проблема исправлена в общей первопричине, а не только в одном caller.
2. Observable behavior соответствует разрешённой normative spec.
3. Добавлен минимальный regression proof либо объяснено, почему он не нужен.
4. Все применимые проверки выполнены; skipped/failed gates явно перечислены.
5. Security, tenant, durability, policy и recovery invariants сохранены.
6. `AGENTS.md` проверен и обновлён, если его факты изменились.
7. Diff не содержит secrets, `.env`, `.idea/`, generated state или unrelated
   user changes.
8. Создан один validated логичный commit, если пользователь явно не попросил не
   коммитить.
