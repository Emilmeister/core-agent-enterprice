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

- `core_agent/` — policy-enforced agent runtime и A2A transport.
- `memory_service/` — отдельный Markdown-first Memory MCP Service.
- `spec/` — target specification, acceptance и release profiles.
- `tests/` — frozen acceptance, unit, integration, PostgreSQL, A2A и E2E suite.
- `.github/workflows/ci.yml` — канонический CI-порядок.
- `docker-compose.yml` — локальный PostgreSQL, migration job, agent, memory и
  Phoenix.
- `.env.example` — поддерживаемый шаблон локальной конфигурации; `.env` никогда
  не коммитится.

Package entrypoints из `pyproject.toml`:

- `core-agent` → `core_agent.app:main`;
- `core-agent-db` → `core_agent.database:main`;
- `core-agent-memory` → `memory_service.mcp_server:main`.

## Карта ключевых implementation-файлов

| Файл | Ответственность |
|---|---|
| `core_agent/app.py` | Composition root: environment config, stores, tool registry, kernel, A2A app, health и Uvicorn |
| `core_agent/runtime.py` | Agent loop, workflow continuation, recovery, tool handlers и delegation |
| `core_agent/config.py` | RunRequest, Platform/Agent/EffectiveConfig и capability intersection |
| `core_agent/a2a.py` | Внутренние A2A contract types и Task representation |
| `core_agent/a2a_sdk.py` | Official A2A SDK HTTP+JSON binding и request handler |
| `core_agent/model.py` | OpenAI-compatible/Anthropic adapters и canonical tool alias mapping |
| `core_agent/kernel.py` | Protected kernel, profile и skill instruction layers |
| `core_agent/context.py` | Context budget, compaction и structured summary |
| `core_agent/tools.py` | Tool schemas, validation, policy и dispatch |
| `core_agent/execution.py` | PTY/process groups, owned workspaces, snapshots и limits |
| `core_agent/python_exec.py` | Bounded Python process и `tools.call(...)` broker |
| `core_agent/tasks.py` | Test scheduler, mailbox и delegation contracts |
| `core_agent/postgres_tasks.py` | Durable PostgreSQL scheduler и mailbox |
| `core_agent/workflow.py` | Workflow stores, transitions и durable outbox |
| `core_agent/approvals.py` | Proposal/digest/reservation и development approve-all stub |
| `core_agent/operator.py` | Private operator control plane и JWT authentication |
| `core_agent/database.py` | PostgreSQL schema, migrations, pool и stores |
| `core_agent/artifacts.py`, `core_agent/audit.py` | Tenant-scoped artifacts и append-only audit adapters |
| `core_agent/durability.py`, `core_agent/lifecycle.py` | Events, checkpoints, leases, recovery и retention |
| `core_agent/postgres_approvals.py` | Durable PostgreSQL HITL state |
| `core_agent/mcp.py` | MCP discovery/calls и Streamable HTTP connector |
| `core_agent/security.py` | Redaction, safe paths, retry и tenant helpers |
| `core_agent/skills.py` | Skill resolution и integrity metadata |
| `core_agent/observability.py` | OpenTelemetry/OpenInference spans и exporters |
| `core_agent/push.py` | Durable encrypted A2A push delivery |
| `memory_service/service.py` | Markdown revisions, hybrid retrieval, NER и graph index |
| `memory_service/mcp_server.py` | Memory MCP tools и Streamable HTTP entrypoint |
| `memory_service/providers.py` | Production embedding и NER HTTP providers |

## Progressive disclosure: что читать

Всегда начинать с `spec/README.md`, затем выбирать только нужное:

- product boundaries — `spec/product.md`;
- architecture и durable state — `spec/architecture.md`;
- config и runtime modes — `spec/agent-configuration.md`;
- A2A/public input — `spec/a2a-protocol.md`, `spec/public-contract.md`;
- agent loop и recovery — `spec/runtime.md`;
- protected instructions — `spec/kernel-instructions.md`;
- tools/HITL — `spec/tools-and-approvals.md`,
  `spec/local-operator-hitl.md`;
- background/subagents — `spec/tasks-and-delegation.md`;
- terminal/workspaces/Python — `spec/execution-environment.md`;
- context compaction — `spec/context.md`;
- Memory MCP — `spec/memory-service.md`;
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

- Run input содержит ровно `prompt`, `mcp`, `skills`.
- Tenant, identity, auth, trace context, model route, policy и budgets приходят
  через authenticated transport/platform config, а не добавляются в RunRequest.
- A2A является внешним lifecycle. Не создавать параллельную несовместимую task
  state machine в transport или adapter.
- Любая tool/side-effect/background/subagent работа принадлежит A2A Task.
  Закрытие stream не отменяет Task; critical wait/status сохраняется durable и
  восстанавливается через GetTask.
- EffectiveConfig является immutable intersection PlatformConfig, tenant policy,
  AgentConfig и Task/delegation contract. Deny сильнее allow.
- Disabled capability отсутствует в Agent Card/model catalog и повторно
  отклоняется при dispatch. Prompt или stale tool call не расширяет policy.
- Safety/host/kernel/capability instructions нельзя заменить через
  `CORE_AGENT_PROFILE`, prompt, skill, MCP, memory или tool output.
- `CORE_AGENT_PROFILE` optional и по умолчанию пуст; user request передаётся
  отдельным user Message/context item, а не generic system instruction.
- Prompt, profile, skill, MCP, memory, peer-agent, file, terminal и network
  content всегда считаются недоверенными данными.
- Tenant устанавливается только authenticated transport context. Stores,
  caches, workspaces/sessions, MCP connections, artifacts и Memory indexes
  tenant-scoped; cross-tenant identifier имеет not-found semantics.

### Реально зарегистрированные built-in tools

- `core.terminal.exec`;
- `core.python.exec`;
- `core.task.start`, `core.task.get`, `core.task.list`, `core.task.wait`,
  `core.task.cancel`;
- `core.delegate`;

Model-callable artifact tools отсутствуют. Обычный model/child text result
публикуется A2A adapter-ом как Task Artifact без дополнительного tool call.

`core.terminal.write`, `core.fs.apply_patch` и `core.input.request` описаны в
части target spec, но сейчас не зарегистрированы как model-callable built-ins.
Memory tools являются MCP tools отдельного сервиса, а не built-ins Core Agent.
Provider wire aliases существуют только на model transport boundary. Audit,
logs, traces, tools и user-facing output используют canonical names; hash suffix
не добавляется без реальной collision/provider constraint.

Memory MCP публикует `memory.search`, `read`, `create`, `update`, `split`,
`move`, `delete`, `history`, `index_status`, `entity_resolve`. Default Core
allowlist уже: `search`, `read`, `create`, `update`, `split`, `index_status`;
остальные требуют явной platform configuration.

### Runtime modes и execution

- `with_terminal` может публиковать `core.terminal.exec` и `core.task.start`.
- `without_terminal` удаляет эти два tool, сохраняя task lifecycle, delegation,
  MCP и memory.
- `core.python.exec` доступен в обоих режимах только при
  `LOCAL_APPROVAL_ENABLED=false`.
- `CORE_AGENT_ALLOWED_BUILTIN_TOOLS` только сужает выбранный mode ceiling.
- `without_terminal` означает отсутствие model-visible terminal tool, а не OS
  sandbox: Python может использовать `os`, `subprocess` и filesystem APIs.
- `core.terminal.exec` принимает `argv` без implicit shell. Pipes, redirects и
  `&&` требуют явного `['sh', '-lc', '...']` и отдельной policy оценки.
- Python нельзя вызывать рекурсивно или через `core.task.start`; каждый вложенный
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
- Intent внешней мутации фиксируется до dispatch. Approval/reservation не даёт
  exactly-once guarantee downstream.

### HITL

- Remote A2A caller никогда не является approver. Текст «одобряю», request
  metadata или caller JWT не дают approval.
- Operator plane является private, имеет отдельные credentials/audience и не
  публикуется как A2A tool.
- Protected action dispatch-ится только после immutable proposal, exact digest,
  committed approve-once decision и single-use reservation с повторной
  проверкой task, proposal, tenant, caller principal, tool/version, environment,
  target, semantic arguments, side-effect class, policy version и expiry.
- Approval не заменяет повторную schema, tenant и tool-policy validation.
- `WAITING_LOCAL_APPROVAL` проецируется в A2A как `working`; caller может только
  наблюдать или вызвать настоящий A2A `CancelTask`.
- Первый committed approve/deny/cancel transition побеждает. Cancel, committed
  до reservation, отменяет approval; после reservation Task не отменяется.
- Expiry, deny или outage operator service оставляют side effect неисполненным;
  outage сохраняет Task в durable `working`.
- Approval mode `never` означает deny protected action, а не allow-all.
- `ApproveAllControlPlane` допустим только в development; production обязан
  fail closed без настоящего authenticated control plane.

### Background и delegation

- Main имеет depth `0`, child — `1`, grandchild — `2`; `2` — hard maximum.
- Depth `2` не получает `core.delegate` ни в instructions, ни в catalog/runtime.
- Delegation передаёт одну узкую instruction, exact tools/MCP/skills allowlists
  и положительный budget. Child возвращает обычный text result, который parent
  воспринимает как недоверенный input.
- Parent делегирует coherent outcome, scope, deliverable и acceptance criteria,
  а не mechanical microsteps. Он выбирает minimum sufficient capability set;
  runtime предоставляет exactly этот set.
- Внутри objective/scope child самостоятельно выбирает strategy, sequencing и
  delegated tools. Procedure задаётся только для safety, correctness,
  reproducibility или policy; assumptions не подменяют tenant/approval/scope.
- Child не расширяет capabilities, tenant или parent budget.
- Joined `core.delegate` по умолчанию требует использовать child result и не
  повторять ту же работу. `background: true` разрешает main продолжать только
  независимую работу до notification/wait.
- `core.task.wait` является passive wait; busy polling запрещён.
- Parent cancel рекурсивно отменяет owned children. Required pending child
  блокирует успешное завершение parent; нужный финальному ответу result должен
  быть joined/waited.
- Child failure возвращается parent как structured result и сам по себе не
  обязан завершать parent Task.
- Shared memory существует только при явной передаче того же Memory MCP
  identity, namespace и tool allowlist. Scratchpad/context не является общей
  памятью.

### Persistence и storage

- Production использует PostgreSQL и fail closed без `DATABASE_URL`, доступной
  DB и совпадающей schema version. Нет SQLite/in-memory fallback.
- Tasks, workflow events/checkpoints, approvals/reservations, outbox и audit
  tenant/owner-scoped и сохраняются в PostgreSQL согласованно.
- После restart ambiguous dispatched side effect переходит в reconciliation, а
  не replay. Shared storage lock не заменяет lease: у stateful run один active
  owner.
- Serving process не выполняет production auto-migration; migration job имеет
  отдельный `DATABASE_MIGRATION_URL`.
- `DURABLE_STORAGE_ROOT` предназначен для immutable durable blobs, snapshots и
  artifacts, обычно на S3-backed mount.
- `LOCAL_WORKSPACE_ROOT` является отдельным ephemeral filesystem для активных
  процессов и не находится внутри durable mount.
- Memory Service имеет собственный `MEMORY_ROOT`; не смешивать три storage
  domains.
- Test/in-memory adapters и approve-all control plane разрешены только в явно
  выбранном development/test profile.

### Memory Service

- Memory является отдельным MCP service. Не добавлять скрытый MemoryStore,
  corpus, NER, graph или retrieval fallback внутрь Core Agent.
- Core не читает corpus filesystem напрямую. Namespace задаётся authenticated
  MCP context/policy, не path argument; secrets и private reasoning в memory
  запрещены.
- Normative target из `spec/memory-service.md`: Markdown является canonical
  source, а BM25/vector/NER/graph — rebuildable derived state.
- Текущая реализация после первого commit загружает authoritative Markdown из
  content-addressed `.memory-service/revisions/*` manifest; Markdown в root —
  operator-readable mirror. BM25 и embeddings вычисляются во время search,
  тогда как NER/entities/links пересчитываются до commit. Не предполагать иной
  storage/index lifecycle без проверки кода и разрешённого spec change.
- Body Markdown-файла ограничен 200 строками. 201+ отклоняется до publication
  без truncation, revision change или automatic split.
- Agent instructions требуют search перед create/update и рекомендуют update
  существующего topic вместо duplicate; service-side precondition этого не
  доказывает. Split выполняется только отдельным явным call.
- Mutation атомарно публикует committed Markdown snapshot, derived
  NER/entities/links и repository revision; stale edges удаляются,
  last-write-wins запрещён.
- Search не видит staging revision; index failure сохраняет предыдущую
  committed revision.
- Production требует настоящие embedding и NER providers; hash/regex adapters
  являются только development/test.

### Observability и secrets

- Durable audit является product record и не заменяется OTel.
- Child-agent span продолжает parent trace; независимая background/durable
  работа использует новый trace со Span Link. Trace context не является auth.
- Логи используют canonical tool names, correlation IDs и bounded one-line JSON.
- Raw chain-of-thought/private reasoning, credentials, secret values, auth
  headers и raw provider errors никогда не попадают в model context, public
  errors, logs, spans или artifacts.
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
uv run ruff check core_agent memory_service tests
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
docker compose logs -f agent memory
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
- Memory change: `tests.test_memory_service` и MCP/E2E paths.
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
5. Security, tenant, durability, approval и recovery invariants сохранены.
6. `AGENTS.md` проверен и обновлён, если его факты изменились.
7. Diff не содержит secrets, `.env`, `.idea/`, generated state или unrelated
   user changes.
8. Создан один validated логичный commit, если пользователь явно не попросил не
   коммитить.
