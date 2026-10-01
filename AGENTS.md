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
7. Проверить, что рабочая копия находится в постоянном каталоге, а не во
   временном хранилище.

## Постоянное хранение рабочих изменений

- Работать только в основном репозитории или Git worktree в постоянной папке.
  Запрещено размещать рабочую копию в `/tmp`, `/private/tmp`, `$TMPDIR` или другом
  каталоге, который может очищаться при перезагрузке либо завершении сессии.
- Единственные экземпляры исходников и незакоммиченных правок должны оставаться
  в постоянной рабочей копии. Временные папки допустимы только для воспроизводимых
  сборок, кэшей и промежуточных результатов.
- Проверенные логические этапы сохранять коммитами согласно commit discipline
  ниже. Отчёт о выполненной работе должен опираться на сохранённые файлы и проверки.

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
- `ui/` — исходники общего owner UI на React/TypeScript/Vite; команды
  `npm ci`, `npm run typecheck` и `npm run build` выполняются из этой папки
  с Node.js 24 (минимум 22.12). Production build создаёт ignored `core_agent/ui_dist/`, который
  входит в Python wheel/sdist; Docker собирает его в отдельном Node stage.
- `spec/` — target specification, acceptance и release profiles.
- `docs/superpowers/specs/` — проектные черновики для согласования; не заменяют
  нормативный `spec/` и не разрешают менять замороженные spec/tests.
- `docs/superpowers/plans/` — планы реализации; не снимают ограничения на
  замороженные spec/tests и не подтверждают готовность runtime capabilities.
- `tests/` — frozen acceptance, unit, integration, PostgreSQL, A2A и E2E suite.
- `.github/workflows/ci.yml` — канонический CI-порядок.
- CI job `sandbox-linux` запускает обязательные Linux sandbox tests в отдельных
  native amd64/arm64 kind-кластерах. Его fixtures из `deploy/kubernetes/`
  назначают контрольные адреса только внутри одноразового node network namespace.
  Node readback проверяет aggregate Pod `pids.max=512`; fork probe ограничен
  513 попытками независимо от большего container-visible cgroup limit.
  `CORE_AGENT_REQUIRE_SANDBOX_TESTS=1` запрещает скрыть недоступный sandbox skip-ом;
  local unit tests не заменяют этот gate и проверку целевого CSI/кластера.
  Dedicated browser gate `tests/test_owner_ui_files_browser.py` использует
  disposable Pod и реальные Keycloak/PostgreSQL. Он обязателен на amd64 с
  `CORE_AGENT_REQUIRE_BROWSER_TESTS=1`: отсутствие Chromium, Node, конфигурации
  или native fixtures завершает проверку ошибкой, без skip.
- `docker-compose.yml` — локальный PostgreSQL, migration job, agent и Phoenix.
- `third_party/skills/` — закреплённые пакеты навыков, происхождение, лицензии и
  контрольные суммы для образа.
- `.env.example` — поддерживаемый шаблон локальной конфигурации; `.env` никогда
  не коммитится.

Package entrypoints из `pyproject.toml`:

- `core-agent` → `core_agent.app:main`;
- `core-agent-db` → `core_agent.database:main`.

## Карта ключевых implementation-файлов

| Файл | Ответственность |
|---|---|
| `core_agent/app.py` | Composition root: environment config, stores, tool registry, kernel, A2A app, health и Uvicorn |
| `core_agent/auth.py` | Keycloak introspection, immutable authenticated scope, owner/external access и SDK context builder |
| `core_agent/admission.py` | Atomic root admission, stable-caller deduplication и busy guard чата до SDK execution |
| `core_agent/interactions.py` | Company settings, per-origin tool policies, CAS и транзакционный запрет pending approvals |
| `core_agent/owner_api.py` | Owner-only settings, policy, HITL/guardrail decisions и приватные ответы на вопросы |
| `core_agent/history.py` | Bounded owner-only проекция полной истории чата, stable cursors и текущие ограничения на материалы |
| `core_agent/cron_expression.py` | Закреплённый croniter parser и ZoneInfo adapter: five-field dialect, bounded calendar search и gap/fold semantics |
| `core_agent/cron.py` | Company schedule/event stores, CAS/receipts и shared atomic root admission, включая fenced agent tool creation |
| `core_agent/cron_service.py` | Bounded recovery-driven coordinator, PostgreSQL company leader session и один memory job на ASGI loop |
| `core_agent/ui.py` | Отдача packaged browser build и точный перечень публичных GET/HEAD assets |
| `core_agent/remote_registry.py` | Company-scoped immutable peer revisions, CAS, encrypted credentials и безопасные metadata для owner API |
| `core_agent/guardrails.py` | Ограниченный classifier без tools, отдельный context и deployment-configured model adapter |
| `core_agent/material_reviews.py` | Private material decisions, detector budget и атомарная связь с guardrail waits |
| `core_agent/chat_files.py` | Private file batches, scoped extraction/download, runtime publication barrier, bounded orphan sweep и atomic raw FilePart admission |
| `core_agent/response_files.py` | Immutable snapshots выбранных файлов результата, scoped manifest и whole-batch integrity validation |
| `core_agent/runtime.py` | Agent loop, workflow continuation, recovery, tool handlers и delegation |
| `core_agent/config.py` | RunRequest, Platform/Agent/EffectiveConfig и capability intersection |
| `core_agent/a2a.py` | Внутренние A2A contract types и Task representation |
| `core_agent/a2a_sdk.py` | Official A2A SDK HTTP+JSON binding и request handler |
| `core_agent/model.py` | OpenAI-compatible/Anthropic adapters, streaming и tool wire payload |
| `core_agent/kernel.py` | Protected kernel, profile и skill instruction layers |
| `core_agent/context.py` | Context budget, compaction и structured summary |
| `core_agent/tools.py` | Tool schemas, validation, policy и dispatch |
| `core_agent/execution.py` | PTY/process groups, owned workspaces, snapshots и limits |
| `core_agent/workspace.py` | Trusted tenant/owner/context binding, постоянные папки чатов и nofollow owner file preview/download |
| `core_agent/workspace_cleanup.py` | Подтверждённая exact selection, durable intent/receipt, private capture/journal и bounded recovery очистки |
| `core_agent/python_exec.py` | Bounded Python process и `tools.call(...)` broker |
| `core_agent/sandbox.py`, `core_agent/sandbox_exec.py` | Gated Bubblewrap launcher, сетевой профиль, inner seccomp и подтверждённая остановка namespace |
| `core_agent/tasks.py` | Test scheduler, mailbox и delegation contracts |
| `core_agent/postgres_tasks.py` | Durable PostgreSQL scheduler и mailbox |
| `core_agent/workflow.py` | Workflow stores, transitions и durable outbox |
| `core_agent/database.py` | PostgreSQL schema, migrations, pool и stores |
| `core_agent/artifacts.py`, `core_agent/audit.py` | Tenant-scoped transport artifacts и append-only audit adapters |
| `core_agent/artifact_service.py` | Named/scoped/versioned artifact model и in-memory, S3, MongoDB backends |
| `core_agent/remote_agents.py` | Legacy ENV registry/SSE, bounded pinned peer discovery и A2A1.0 JSONRPC/HTTP+JSON Send/Get/Cancel adapter |
| `core_agent/remote_operations.py` | Один bounded Send/Get/Cancel step под scheduler claim, pinned credentials, deadline и reconciliation |
| `core_agent/streaming.py` | Stream chunk merge, snapshot buffer и ADK metadata keys |
| `core_agent/durability.py`, `core_agent/lifecycle.py` | Events, checkpoints, leases, recovery и retention |
| `core_agent/mcp.py` | MCP discovery/calls, canonical tool naming и Streamable HTTP connector |
| `core_agent/security.py` | Redaction, safe paths, retry и tenant helpers |
| `core_agent/skills.py` | Обнаружение, закрепление и безопасное чтение ресурсов навыков |
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

- `CORE_AGENT_ENVIRONMENT` обязателен и принимает только `production`,
  `development`, `test`. Production требует полный набор `KEYCLOAK_*` и
  `CORE_AGENT_TENANT_ID`; legacy A2A без auth разрешён только при явно выбранном
  development/test и полностью отсутствующей auth-конфигурации.
- Настроенный Keycloak открывает `/a2a/owner/`, `/a2a/external/` и owner-only
  `/api/identity`; старые корневые A2A routes закрыты. Probes остаются публичными.
  Introspection выполняется один раз на HTTP request без кэша; уже открытый
  ответ не проверяется заново. Incoming credentials не передаются remote agents.
- Company scope задаётся deployment config и не меняется SDK tenant metadata.
  Владельцы используют общий scope, внешний caller — стабильный issuer/sub.
  External role исключает owner authority. Owner-wide доступ к задаче сохраняет
  её исходного owner; actor identity отдельно записывается в admission audit и
  follow-up provenance. Cancel проверяет scoped Task до active SDK registry.
- Legacy development/test memory adapter связывает anonymous A2A scope с
  настроенным `USER_ID` и runtime tenant `default` только для canonical workflow
  projection; проверка доступа к SDK Task предшествует этой проекции.
- Авторизованные A2A endpoints принимают новый root через общий admission:
  один нетерминальный root на чат, duplicate по `(tenant, actor, messageId)`
  проверяется до busy guard. Изменённый Message даёт `MESSAGE_ID_CONFLICT`;
  новая попытка в занятом чате сохраняется как отдельная failed Task с
  `CONTEXT_BUSY`, без workflow или worker. Owner сохраняет исходный scope чата.
- Initial Message, Task, chat mapping, request ledger и workflow записываются
  одной PostgreSQL транзакцией до запуска SDK. Shutdown или потеря stream
  после commit оставляет Task для recovery. Legacy context без явного mapping
  не присваивается новому caller. Enterprise raw FileParts проходят bounded
  pre-SDK validation и atomic file admission; URL Parts отклоняются.
- Внутренний file admission выполняет preflight под canonical locks, освобождает
  соединение до settings/staging и повторяет проверки при commit. Bind batch
  входит в root/inbox transaction; нет вложенного захвата PostgreSQL pool.
  Проигравший private stage очищается после выхода из transaction; cleanup
  failure оставляет прежние age/lease для sweeper, не меняя original receipt.
  Initial snapshot и follow-up provenance получают только server-owned batch IDs.
  File receipt содержит фактические безопасные имена, пути, размеры и digest;
  исходные имена и metadata сохраняются в private immutable batch для guardrails.
  PostgreSQL Task записывается до file bind в той же admission transaction.
  PostgreSQL manifest хранит точные originals/metadata как escaped JSON text,
  сохраняя schema version и безопасные имена в native fields; reader также
  поддерживает прежние raw manifests без перезаписи принятых rows.
  Memory admission берёт async locks до синхронного workflow/Task commit без await.
- File service создаётся до recovery. Startup/hourly bounded sweep использует
  original age, немедленно продолжает backlog и не удаляет accepted quarantine.
  Временная ошибка публикации принятого immutable batch сохраняет pending
  delivery для recovery вместо ложного отказа приёма; model input ждёт публикации.
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
- `LLM_EXTRA_BODY_JSON` не задаёт каталог или выбор tools: adapter удаляет
  оттуда `tools`, `tool_choice`, legacy `functions` и `function_call` и передаёт
  только текущий runtime catalog, в том числе пустой каталог детектора.
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
- `core_wait_until` — время пробуждения с явным UTC offset; новое сообщение
  будит текущий таймер, повтор сообщения не будит следующую generation;
- `core_ask_owner` — private вопрос владельцам при подключённом owner plane;
- `core_delegate`;
- `core_artifact_save`, `core_artifact_load`, `core_artifact_list`;
- `core_response_files` при подключённом enterprise owner plane;
- `core_memory_search`, `core_memory_read`, `core_memory_create`,
  `core_memory_update`, `core_memory_split`, `core_memory_delete`;
- `core_agent_send_message`;
- `core_cron_create` при подключённом enterprise cron store;
- условные `core_skill_activate` и `core_skill_read_resource`.

`core_skill_activate` появляется в каталоге модели только при непустом наборе
навыков в `EffectiveConfig`, а `core_skill_read_resource` — только после
подключения навыка с объявленными ресурсами. Это служебные инструменты
поэтапного раскрытия, а не самостоятельные возможности: они не входят в
`CORE_AGENT_ALLOWED_BUILTIN_TOOLS` и не передаются отдельно в
`core_delegate.tools`. Их вызовы расходуют общий лимит вызовов инструментов.
Оба имени зарезервированы runtime: совпадающее каноническое имя MCP-tool
отклоняется с `TOOL_NAME_COLLISION` при построении `EffectiveConfig`.

Artifact tools версионируют именованные файлы внутри агента; `user:`-префикс
даёт cross-session scope, а `ARTIFACT_STORAGE_TYPE` выбирает in-memory, S3 или
MongoDB backend без управления схемой внешнего хранилища. Хранилище не является
файловой системой run-а: `core_artifact_save` принимает либо `content`, либо
`path` файла в workspace, который runtime читает сам. `core_agent_send_message`
в authenticated deployment создаёт durable local handle для доверенного peer
из company registry. `core_task_wait` ждёт его сохранённый outcome без нового
timeout. Legacy direct/test runtime без registry сохраняет ENV adapter отдельно.
Обычный model/child text result по-прежнему
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

### Пакеты навыков

- Образ содержит семь пакетов только для чтения из `third_party/skills/`;
  при сборке проверяются `SHA256SUMS`, отсутствие символических ссылок и
  отсутствие прав на запись у пользователя `agent`.
- Перед каждым обращением к модели контекст содержит имена и краткие описания
  всех и только навыков из `EffectiveConfig`. Тело неактивного `SKILL.md` и его
  ресурсы не раскрываются.
- Модель выбирает минимальный набор по смыслу описаний. Совпадение имени с частью
  запроса или специальная команда не подключают навык автоматически.
- Успешный `core_skill_activate` закрепляет контрольную сумму и добавляет полное
  содержимое со следующего хода. Повторное подключение не дублирует инструкции.
- Запрос подключения является границей хода: последующие вызовы из того же
  ответа не выполняются, получают `SKILL_ACTIVATION_BOUNDARY` и требуют нового
  решения модели с полной инструкцией. Их попытки учитываются общим лимитом.
- Пока подключение доступно, потоковый текст и рассуждение удерживаются до
  классификации полного ответа и отбрасываются, если ответ запросил подключение.
- `core_skill_read_resource` принимает только идентификатор из перечня ресурсов
  активного навыка, читает ограниченный по размеру текст UTF-8, возвращает
  контрольную сумму и не запускает сценарии.
- Новая задача принимает только закреплённые контрольную сумму `SKILL.md` и
  полный перечень ресурсов; среда проверяет их до модели и при обращении.
  Старый активный снимок продолжает только сохранённые инструкции без чтения
  текущего пакета. После подключения бюджет контекста рассчитывается заново.
- Содержимое навыков и ресурсов считается недоверенным: оно не повышает
  приоритет инструкций, не расширяет `EffectiveConfig` и не открывает пути вне
  закреплённого пакета.
- Дочерний агент получает доступ только к навыкам из поля `skills`, переданного
  при делегировании. Подключённые родителем навыки он не наследует; служебные
  инструменты выводятся из этого списка автоматически.

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
При recovery сохранённый MCP catalog задаёт неизменный ceiling и digest, а
текущая discovery — доступность и schema внутри него. Исчезнувший tool скрыт;
изменение schema/origin после HITL возвращает `TOOL_APPROVAL_STALE` без dispatch.

### Runtime modes и execution

- `with_terminal` может публиковать `core_terminal_exec` и `core_task_start`.
- `without_terminal` удаляет эти два tool, сохраняя task lifecycle, delegation,
  MCP и memory.
- `core_python_exec` доступен в обоих режимах, если не удалён
  `CORE_AGENT_ALLOWED_BUILTIN_TOOLS`.
- Произвольный Python классифицируется как потенциально mutating; неизвестный
  outcome после старта требует reconciliation, а не повторного выполнения кода.
- `CORE_AGENT_ALLOWED_BUILTIN_TOOLS` только сужает выбранный mode ceiling.
- `CORE_AGENT_BUDGET_CANCEL_GRACE_SECONDS` задаёт положительное bounded ожидание
  подтверждения cancel owned Tasks перед возвратом budget-partial результата.
- `without_terminal` означает отсутствие model-visible terminal tool. Python
  может использовать `os`, `subprocess` и filesystem APIs внутри того же sandbox.
- `core_terminal_exec` принимает `argv` без implicit shell. Pipes, redirects и
  `&&` требуют явного `['sh', '-lc', '...']` и отдельной policy оценки.
- Образ v1 содержит основной CLI-набор для текста, файлов, структурированных
  данных, архивов, сети и PDF, включая Mike Farah `yq` и команду `fd`. Краткий
  model-facing перечень не является allowlist; non-root agent может добавлять
  инструменты только в workspace и только при разрешённых policy и сети.
- Группа зависимостей `python-tool` устанавливается в системный
  `/usr/local/bin/python3`, который запускает `core_python_exec`, отдельно от
  `/app/.venv`. В неё входят библиотеки для HTTP, проверки и разбора данных,
  HTML/XML, PDF, Office, изображений и таблиц, включая DuckDB, NumPy и pandas.
- Python нельзя вызывать рекурсивно или через `core_task_start`; каждый вложенный
  `tools.call` заново проходит EffectiveConfig, schema, policy, общий budget,
  owner/tenant, audit и OTel.
- `create_app` обязательно проверяет SandboxPolicy/SandboxLauncher до recovery
  и listener, включая development. Без Linux primitives нет runtime fallback.
  `tests/app_support.py` явно подставляет переносимый adapter для unit tests;
  native `tests/test_sandbox_linux.py` использует настоящий composition root.
- Terminal, Python и background target используют один launcher с отдельными
  mount/PID/network namespaces. Доступен только текущий `/workspace`, вложения
  монтируются read-only; Python получает один принадлежащий вызову broker socket.
- Cancel/timeout подтверждает завершение namespace до успешного cleanup result.
  Ошибка teardown блокирует snapshot/delete; unhealthy launcher закрывает readiness.
  `SANDBOX_DNS_SERVERS` и `SANDBOX_DENIED_CIDRS` обязательны и дополняют закреплённую
  `core_agent/sandbox-policy.json`, которая входит в wheel и root-owned image.
  Slirp входит только в owned user namespace, сохраняя Pod netns для egress;
  target gate ждёт IPv6 address/default route до исходного startup deadline.
  Base default-deny и TTY injection deny — отдельные sealed seccomp filters,
  которые bwrap загружает совместно через `--add-seccomp-fd`.
  Наличие кода не подтверждает native/target-cluster gate.
- Process ownership привязано к `(run_id, worker_id, execution_generation)`.
  Закрытая generation не открывается после follow-up; старая попытка не удаляет
  новую session. Session teardown сериализован и идемпотентен. При nonterminal
  suspension сохраняются и ephemeral run-файлы; их инициализация из snapshot
  публикуется атомарно, постоянные chat-файлы не восстанавливаются поверх live данных.
- До terminal transition runtime сохраняет terminal intent и закрывает admission
  новых процессов и дочерних задач во всём дереве. Чат остаётся занят до
  подтверждения cleanup каждой сохранённой execution generation; сигналы и
  ожидание процессов выполняются вне транзакции БД. Foreign receipt с
  `local_execution_pending=true` или без этого поля требует reconciliation.
  Явное `false` позволяет восстановить model-only или безопасно остановленное
  ожидание. Принятый follow-up переоткрывает Task с новой lease; старый intent
  не может закрыть новую generation или её cached runtime/connector.
  Сам terminal intent не закрывает inbox: до фактического terminal commit
  follow-up принимается, включая private file batch. Публикация файлов остаётся
  запрещена до возобновления; failure/cancel сохраняет недоставленный input
  с disposition вместо запуска новой работы.
- Docker, Kubernetes, A2A, OTel и
  platform credentials никогда не передаются child process. Разрешённый
  task-specific secret приходит как policy-approved `secret_ref`, materialize-ится
  непосредственно для одного process и не попадает в argv/checkpoint/telemetry.

### Tool failures и side effects

- Tool arguments валидируются до policy и dispatch.
- Terminal и Python всегда потенциально mutating. Owner allow и guardrails
  exemption не разрешают повтор при неизвестном исходе. Вложенная Python
  мутация с неизвестным outcome durable требует `SIDE_EFFECT_UNKNOWN`; terminal
  `ABORTED` допустим после подтверждённой очистки owned processes. Неизвестная
  очистка оставляет nonterminal барьер. Python `try/except` не скрывает этот
  исход или исчерпание budget от runtime.
- Доказанная pre-dispatch/schema/start error или определённый
  failed/timed-out outcome возвращается модели как structured tool result, чтобы
  она могла исправиться или объяснить ошибку пользователю.
- Неизвестный outcome возможного mutating side effect требует
  `SIDE_EFFECT_UNKNOWN`/reconciliation и никогда не получает blind retry.
- Intent внешней мутации фиксируется до dispatch. Runtime не обещает
  exactly-once guarantee downstream.

### Context и compaction

- Base включает system/kernel/profile, tool schemas и output reserve; pinned
  сравнивается с оставшейся working capacity даже без summarizable history.
- Неудачное interval compaction ниже pressure сохраняет исходный context;
  lease/storage ошибки не поглощаются. Overlap освобождается, если занимает
  всю цель; pinned выше цели получает реальное оставшееся место для summary.
- Production `StructuredSummarizer` получает отдельный model callback без tools
  и проверяет семь JSON-секций, basis и immutable source IDs. Runtime до каждой
  физической попытки сохраняет marker и списывает общий/local model budget;
  максимум две попытки, reserved finalization turn не расходуется на summary.
- Provenance и current material decisions проверяются до compaction и model
  call. Summary с запрещённым источником целиком исключается; допустимые originals
  восстанавливаются без provider replay и недоставленных failure/cancel inputs.
  Полный private transcript сохраняется. Compaction operation и source fingerprint
  переживают recovery; неизвестный outcome не обнуляет оплаченные попытки.
- Canonical root admission закрепляет `previous_root_run_id` в начальном
  snapshot до смены latest root. Duplicate и busy Task цепочку не меняют;
  request metadata не задаёт источник. Legacy отсутствие поля означает, что
  previous root не закреплён.
- После проверки нового prompt runtime импортирует предыдущую terminal root
  только с теми же tenant/owner/context. Старые цели, tool data и outcome идут
  как plain history без provider replay; новая инструкция остаётся pinned.
  `context_import` version 1 фиксируется под текущей lease до модели и не
  повторяется при recovery. Full transcripts источников не копируются.
- Исторические originals читаются по исходному run/sequence; final result имеет
  отдельную identity по run и digest полного persisted result. Current rejection
  проверяется и для dependencies, и для самого итогового текста. Foreign reads
  используют текущую transaction connection без writer locks завершённых задач.
  Owner history API использует отдельную read-only проекцию полного transcript;
  imported model context и summary не являются пользовательской историей.

### HITL

- `KEYCLOAK_UI_CLIENT_ID` задаёт отдельный public browser client. Только при
  непустом значении публичен точный `/ui/config`, возвращающий issuer/client ID
  без confidential credentials. При наличии regular build в
  `core_agent/ui_dist/` также доступны shell и перечисленные assets; symlinks и
  произвольные пути не выдаются. Exact public allowlist действует только для
  GET/HEAD; `/api/` и оба A2A входа сохраняют server-side authorization.
- Authenticated composition root подключает company settings и per-tool policy
  до запуска recovery. `/api/chats`, `/api/settings`, `/api/tool-policies`, `/api/interactions`,
  `/api/hitl/{wait_id}/decision`, `/api/questions/{wait_id}/answer` и
  `/api/guardrails/{wait_id}/decision` и `/api/guardrails/{wait_id}/material`
  доступны только verified owners.
  Owner API не запускает tool: фиксирует одно решение, runtime продолжает через
  durable wait. Material route получает tenant из principal, run/owner из
  сохранённого wait; query parameters не могут подменить scope.
- `/api/chats` читает canonical company mapping и последний root без запуска
  workflow. Owners видят общий список; external/dual-role отклоняются до lookup.
  Pagination cursor ссылается на сохранённую root Task и сохраняет позицию при
  появлении новой Task того же чата; длинный context ID не увеличивает cursor.
- `/api/chats/{context_id}/history` читает canonical previous-root chain, полный
  transcript и сохранённые inbound Messages с bounded pagination, не запуская
  workflow, модель или classifier. Cursor привязан к company/chat и сохраняет
  позицию при новых Task, compaction и доставке queued input. Server anchors и
  delivery provenance сохраняют identity сообщения без двойного отображения.
  PostgreSQL читает проекцию в read-only repeatable-read transaction. Provider
  replay, hidden reasoning и private material bytes не выдаются. Current negative
  decisions проверяются для originals, dependencies и final text; digest всегда
  сопоставляется вместе с material kind. Запрещённый материал заменяется safe
  placeholder со ссылкой на owner review; external/dual-role доступа не получают.
- Owner file preview/download получают original WorkspaceBinding из canonical
  admission mapping; просматривающий owner не становится execution owner.
  Nofollow directory fds исключают symlinks, hardlinks и special files; private
  staging/quarantine и служебные manifests не выдаются. Read не создаёт папку,
  не запускает workflow и доступен при активной Task. Bounded preview cursor
  подписан process-local key, связан с company/chat/filter/directory и одним
  server timestamp; restart требует нового preview. Это отдельный контракт
  от durable history cursor. File descriptor закрывается при любом окончании
  download, включая disconnect. Preview не означает готовую cleanup/delete
  capability: удаление требует отдельного chat-lock/revision/tombstone path.
- `/api/remote-agents` предоставляет owner-only list/create; PUT/DELETE по server
  ID создают новую immutable revision через CAS. DELETE отключает peer, сохраняя
  прежние revisions. Scope берётся из principal; external/dual-role запрещены.
  Header values принимаются только на запись; responses/errors/cursors содержат
  безопасные metadata. Fernet envelope связывает secret с tenant/peer/revision/
  header name. Key — `PUSH_NOTIFICATION_ENCRYPTION_KEY`; in-memory development
  допускает ephemeral key, PostgreSQL secret operations требуют persistent key.
  Authenticated composition использует registry как единственный authority;
  ENV discovery и incoming auth forwarding здесь не выполняются. Legacy ENV
  переносится только отдельным явным import; автоматического import пока нет.
  Peer revision, message ID и settings закрепляются до HITL в versioned parent
  snapshot; atomic scheduler admission возвращает тот же handle после recovery.
- Новые методы `RemoteAgentConnection.send_task/get_task/cancel_task` работают
  с обоими A2A1.0 bindings. `connect_peer` сверяет объявленный Card interface
  с зарегистрированным полным URL path/params; redirects запрещены. Только
  read-only discovery имеет bounded retry; Get temporary5xx планирует следующую
  проверку в пределах deadline; Send/Cancel имеют одну attempt.
  Per-peer headers передаются на один request без хранения в connection;
  caller/root identifiers и legacy auth не наследуются. Parts и remote IDs
  сохраняются executor-ом для durable operation. Enterprise Send принимает
  optional `files` — уникальные относительные workspace paths на каждый вызов;
  отсутствие поля или `[]` отправляет только текст. Выбор финальных вложений
  `core_response_files` не используется автоматически. Весь набор snapshot-ится
  до HITL; ошибка сохраняет прежний final set и не создаёт remote handle.
  Approval получает только safe receipts и digest выбора, без private refs.
  Перед Send весь frozen manifest проверяется повторно, затем передаются только
  стандартные raw Parts с именем/media type, включая пустые файлы.
  Responses проходят bounded strict JSON/canonical base64 до SDK; decoded
  aggregate относится к текущему ответу, а не к прежним файлам в Task history.
  Legacy ENV/text-only schema не получает новый аргумент `files`.
- Owner settings GET/PUT включают `remote_timeout_seconds` (86400 по умолчанию)
  и `remote_poll_interval_seconds` (300). PUT со старым набором трёх timeout
  сохраняет эти настройки и attachment limit; все поля делят settings revision.
- `attachment_limit_bytes` в company settings задаёт общий decoded aggregate
  лимит вложений (default 25 000 000). Timeout-only PUT сохраняет прежний лимит;
  принятые manifests не пересматриваются при его изменении.
- `A2A_MAX_REQUEST_BYTES` отдельно ограничивает encoded HTTP body до SDK
  (default 40 000 000 bytes). Для большего company limit deployment ceiling
  увеличивается с учётом JSON/base64 overhead; старый принятый dedup не зависит
  от нового company limit. Raw FileParts доступны на owner/external routes;
  файлы публикуются только после полного batch guardrail решения.
- Доступные owner history messages после file publication содержат только
  actual name/path/size/digest из scoped batch. Quarantine/excluded names не
  раскрываются через history; bytes и исходная metadata остаются в private store.
- `core_response_files` принимает полный список относительных workspace paths,
  замещает предыдущий выбор или очищает его через `[]`. Ошибка сохраняет прежний
  набор. Snapshot и receipt фиксируются одним durable переходом, включая Python
  broker; bytes сохраняются в transport store, не в workflow JSON/model context.
  Новые выборы используют текущий company limit, принятый набор закрепляет его.
  Final A2A Artifact выдаёт typed raw Parts; live/GetTask/subscription/recovery
  проверяют весь canonical manifest и blobs до выдачи. Terminal Message не
  дублирует файлы. Owner final history содержит только safe `response_files`
  receipts, без blob IDs/raw bytes; download по chat/Task/file ID снова проверяет
  текущие права и integrity всего набора и не читает изменённый workspace source.
- File guardrail download `/api/guardrails/{wait_id}/file` получает точный
  sealed reference из сохранённого review, возвращает только owner-scoped
  проверенные bytes как attachment и остаётся доступен для owner history.
- Composition root подключает material store и отдельный classifier до recovery.
  Default использует непотоковую копию подключения модели агента; четыре
  `GUARDRAILS_LLM_*` overrides требуют полного набора и не наследуют credentials
  основной модели. Detector имеет отдельные time/token/call limits; его tools
  всегда пусты. File quarantine подключён к внутреннему canonical admission;
  public raw FileParts принимаются через scoped admission после native sandbox proof.
- Initial/follow-up input, owner answers, tool args/results, background и nested
  Python проходят runtime material gates. Известный результат сохраняется до
  review; возобновление раскрывает его без повторного dispatch. Pending nested
  review и ошибка его persistence сначала останавливают Python, поэтому код не
  может перехватить host-side отказ и продолжить выполнение.
- Отклонённый/просроченный exact material проверяется по сохранённым решениям
  того же tenant/owner/chat до exemption и новой классификации, включая другой
  tool call или новый run. SDK progress не публикует raw call/results, provider
  reasoning или непроверенные remote frames; owner читает их через private API.
- Direct model calls используют `allow|require_hitl|deny`, новый tool требует
  HITL. Deny скрывает tool из последующего model catalog и проверяется перед
  dispatch; переключение в allow не разрешает уже открытый запрос. Отказ и
  timeout возвращаются модели как tool results. Background target получает
  собственный workflow и approval без отдельного model loop. Nested Python
  сохраняет точный broker call, останавливает процесс до safe wait и после
  решения исполняет только этот call. Prefix и remainder не повторяются; модель
  получает interrupted result под исходным outer tool-call ID. Unit proof
  не заменяет обязательный native Linux gate.
- `core_ask_owner` доступен при подключённом owner plane; создаёт private question
  после собственной policy/HITL. External-owned Task публикует generic status и
  итоговый ответ; внутренние вопросы, ответы и промежуточный tool/model stream
  остаются приватными.
- Remote A2A caller не становится approver через текст, request metadata или
  caller JWT. Не документировать модули approval/operator как включённый
  runtime flow, пока они не подключены в `core_agent/app.py` и не имеют CI proof.

### Background и delegation

- Remote kind `remote_a2a` использует existing scheduler/ownership/mailbox
  и mutable checkpoint LONG-02. Прежний immutable contract v1 читается без
  изменений. Enterprise contract v2 закрепляет source `caller_scope` ровно
  `{owner_id, context_id, task_id, run_id}`, aggregate limit и frozen file refs;
  scheduler owner совпадает с source run ID. Child сохраняет свои task/run IDs.
  Parent snapshot wrapper остаётся v1, entry v2 дополнительно закрепляет digest
  исходных аргументов: recovery не читает изменённые workspace paths заново.
  Unknown versions/fields/scope и изменённые arguments отклоняются.
  Local rows имеют null checkpoint. Миграции выполняет database entrypoint,
  а не serving process.
- Send/Cancel marker сохраняется до network; один worker выполняет один bounded
  request и освобождает claim. Unknown Send без remote ID и уже сохранённый
  Cancel marker требуют reconciliation; они не повторяются после recovery.
  Shutdown не является caller cancellation. Временные Get ошибки планируют
  следующую проверку в пределах прежнего deadline; timeout закрывает operation
  атомарно и выигрывает у позднего ответа.
- Executor читает credentials только из закреплённой registry revision.
  Отражённые секреты в peer IDs отклоняются до checkpoint/URL, текст очищается
  existing redactor. v2 `working`/`input-required`/`auth-required` не импортируют
  ранние raw previews и сохраняют polling с прежними IDs/deadline. Terminal v2
  files принимаются в quarantine через один ChatFileService, подключённый
  composition root к executor/scheduler/runtime; отсутствие service явно закрывает
  import. Runtime refetch-ит только owned remote completed Task и закрепляет
  полный text/ordered file receipt до guardrails. Frozen progress не повышается
  до terminal outcome при повторном чтении scheduler state.
  v1 file response остаётся `REMOTE_FILES_UNSUPPORTED`. Accepted batch хранит
  source run и derived public root Task; child public rows не создаются.
  Shared `caller_scope` держит chat → root → intermediate → source locks,
  использует actual parent links максимум двух уровней и fence только source
  writer lease; historical sealed reads допускают terminal ancestors.
  Quarantine bind, terminal result и notification/outbox commit-ятся атомарно
  под claim/deadline/revision/cancel fences на одной borrowed connection.
  Private `file_batch_id` исключается из remote mailbox/outbox и model task
  snapshots. Text и каждый файл проходят guardrails до whole-batch publication;
  отказ исключает весь result, task list может сохранить отдельно разрешённые
  результаты. Вложенный Python останавливается перед ожиданием, recovery не
  повторяет Send или prefix кода; provenance всех file receipts сохраняется
  даже для bounded outcomes. Trusted-tool exemption фиксируется durable до
  публикации и не отменяет предыдущий отказ по точному материалу.
  Migration23 разрешает только superseding denial `accepted_ready → excluded`.
  Recovery возвращает уже перенесённый, но не committed каталог в private
  quarantine и синхронизирует оба родителя до exclusion; published batch и
  остальные decision references неизменяемы.
  Reflected credentials в metadata/raw files отклоняются до stage, проверяя
  UTF-8 и фактические Latin-1 header bytes; содержимое файлов не переписывается.
  Data Parts отклоняются. Ошибка network/parser после возможного
  Send/Cancel сохраняет unknown outcome; только доказанная локальная ошибка
  до dispatch возвращается как известный отказ без blind retry.
- TaskStore проецирует `core_agent_remote_progress` из scoped working operations
  как local ID, stable displayed revision, peer name и nonterminal enum. Read-only
  `remote_progress` не вызывает model-visible task tools. Get/List/Subscribe/push
  сохраняют одинаковую проекцию, включая stale live Task frames; terminal и
  просроченные операции исчезают даже до coordinator tick. Повторный status не
  создаёт event/push и не будит модель, не меняет budget или deadline.

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
- Direct model calls `core_task_wait`, `core_wait_until` и joined `core_delegate`
  сохраняют continuation в `core_waits` и возвращают `SuspendedRun`. Он не
  сериализуется как tool result или Artifact. A2A остаётся `working`, workflow
  lease и scheduler claim освобождаются; coordinator возобновляет только
  resolved wait без повторного списания tool budget. Follow-up будит только
  timer; при ожидании Task он доставляется после результата ожидания.
- Python nested suspension использует `python_execution` и continuation
  `python_nested` для HITL, owner question, timer, task wait и joined delegation.
  Это не checkpoint CPython. Broker переносит trusted execution context в свой
  thread и удерживает ответ до подтверждения остановки; неподтверждённый stop
  не превращается в catchable ошибку или EOF для живого interpreter.
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

### Cron

- Авторизованная enterprise composition подключает owner-only `/api/schedules`
  CRUD и `/{id}/run-now`. У каждого расписания один canonical chat; create без
  context создаёт пустой owner chat, а не фиктивную Task. Chat list и UI принимают
  `latest_task_id: null`; длинные прежние contexts сохраняют legacy task cursor.
- `core_cron_create` доступен в обоих modes только внутри effective ceiling,
  live tool policy и явного child allowlist; default HITL действует как для других
  mutating tools. Run/call/attempt receipt и lease/cancel fence задаются server-ом,
  аргументы не выбирают tenant, owner или context. Unconfigured legacy runtime
  не публикует tool. Agent origin не является owner authority.
- Каждая cron Task проходит общий root admission. Admission/event/next due
  automatic occurrence сохраняются одной транзакцией; manual run не меняет next
  due. Busy, late, downtime и pending cleanup дают immutable skip notice без Task
  и очереди. Delete/disable не отменяют принятые roots или их waits.
- Календарь использует закреплённый `croniter==6.2.4`, five-field validation и
  `ZoneInfo` gap/fold adapter. Default timezone `Europe/Moscow`; aware UTC next due
  строго позже текущего instant. См. CRON-05/08 в нормативной спецификации.
- Existing recovery tick запускает bounded coordinator pass. Production leader
  держит company session advisory lock на выделенной PostgreSQL connection; на
  ней же коммитятся automatic occurrences. Memory tick только планирует один job
  на ASGI loop. Startup/re-leadership/gap >60 секунд фиксируют cutoff, catch-up
  отсутствует; healthy lateness до60 секунд допускается. Shutdown не отменяет
  admitted Tasks. No provider call inside schedule/admission transactions.
- Owner history включает только safe `schedule_notice` проекцию skipped events,
  с immutable root/position anchor и version2 event cursor; issued version1 и
  прежние ordinary entry IDs сохраняются. Notices не становятся model user turns.

### Persistence и storage

- Production использует PostgreSQL и fail closed без настроенного DSN, доступной
  DB и совпадающей schema version. Нет SQLite/in-memory fallback.
- Tasks, workflow events/checkpoints, inbound inbox, outbox и audit
  tenant/owner-scoped и сохраняются в PostgreSQL согласованно.
- `core_chats.latest_root_run_id` указывает на последний root; занятость
  определяется его canonical workflow state. Terminal transition не очищает
  указатель. `core_root_messages` хранит immutable creation dedup ledger без TTL.
  Schema 15 включает durable waits, company settings и per-origin tool policies; migration выполняется отдельной
  job после остановки старых workers;
  после новых записей откат image требует согласованного отката БД.
  Legacy run-family retention отвечает `RETENTION_PROHIBITED` для enterprise
  чатов до изменения данных; очистка workspace не удаляет историю или ledger.
- Schema 22 добавляет company-scoped cron schedules и append-only events с
  version1, CAS и immutable identity; canonical chat tenant/context/owner keys
  защищены отдельным trigger. Serving role не удаляет schedules/events и не
  изменяет events. Canonical chat writers используют `FOR NO KEY UPDATE`, чтобы
  сохранять взаимное исключение, разрешая FK `KEY SHARE` при fenced tool create.
  Rollback после новых cron records требует совместимого reader либо DB restore.
- Schema 21 сохраняет immutable cleanup intent и chat workspace revision.
  `/api/chats/{context_id}/files/delete` доступен только владельцам: POST принимает
  точный подтверждённый набор с request ID, GET пассивно читает receipt. Active
  root отклоняет новую очистку без intent/очереди. Pending/reconciliation intent
  блокирует новые roots под тем же canonical chat lock; accepted duplicate Task
  остаётся доступной. HTTP send/stream получает safe retryable 503, JSON-RPC —
  server error -32000. Preview cursor/identity version2 закрепляют revision.
  Existing recovery coordinator продолжает committed intent bounded проходами,
  пропуская требующие ремонта операции без starvation остальных чатов.
  Private capture/journal находится вне model workspace; missing source без
  verified deletion proof не считается успешным удалением. Откат после intent
  требует совместимого reader либо согласованного восстановления DB+volumes.
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
- `CHAT_WORKSPACE_ROOT` задаёт постоянный POSIX root папок чатов и обязателен
  при Keycloak authentication. Все три storage roots не пересекаются ни в одном
  направлении. Anonymous development/test без него сохраняет ephemeral режим.
- Runtime связывает root, child, background и recovered run с исходными
  tenant/owner/context. Authenticated binding дополнительно проверяется по
  `core_chats`; legacy workflow без mapping не получает папку автоматически.
  TerminalSessionManager требует `bind_run` до `execute_transient`;
  явный низкоуровневый `create(EnvironmentSpec(...))` поддерживает scratch.
  Destroy сессии сохраняет папку чата, `LOCAL_BASE_SNAPSHOT` её не перезаписывает.
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
- Auth change: `tests.test_auth` на in-memory и PostgreSQL, а также
  `tests.test_keycloak_integration` с настоящим локальным Keycloak. Последний
  требует `TEST_KEYCLOAK_URL`, `TEST_KEYCLOAK_ADMIN` и
  `TEST_KEYCLOAK_ADMIN_PASSWORD`, создаёт и удаляет только свой временный realm.
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
