# Enterprise cron Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Preserve explicit file ownership; do not edit admission/workflow/runtime/history while another worker owns them.

**Goal:** Владелец и одобренный инструмент создают общие company-scoped расписания, каждый запуск которых проходит обычный root admission в одном постоянном чате; перекрытия и downtime оставляют durable записи о пропуске.

**Architecture:** Добавить небольшой cron store/service к существующим PostgreSQL root admission, workflow recovery и owner API. Расписание выбирает момент и параметры новой Task; её выполнение, HITL, guardrails, cancel, budgets и recovery остаются существующими. Admission и продвижение расписания коммитятся одной транзакцией; отдельного broker, job executor или lifecycle нет.

**Tech Stack:** Python, PostgreSQL/psycopg, существующие unittest/ASGI/React/TypeScript; stdlib `zoneinfo`; `croniter==6.2.4`, закреплённый настоящим offline resolution в `uv.lock`.

## Статус, полномочия и источники

Это план и журнал проверяемых срезов, не свидетельство готовности релиза.
Parser/DST, storage/schema22, shared admission, coordinator, owner history и API
реализованы в source и имеют профильные memory проверки/independent reviews.
UI management/empty chats/skip notices подключены в source; agent tool slice
завершается отдельно. PostgreSQL concurrency/migration/leader gates и настоящий
browser/build gate остаются обязательными открытыми проверками.
Нормативные источники: `spec/tasks-and-delegation.md` CRON-01–10, TASK-02,
`spec/tools.md` «Создание cron», `spec/agent-configuration.md` UI-02,
`spec/context.md` CONTEXT-01/03, `spec/architecture.md`, `spec/public-contract.md`,
`spec/acceptance.md` ENT-AC-22–24, 52–56 и 65.
Разрешение пользователя на frozen spec/tests от 29 сентября записано в
`docs/superpowers/plans/2026-09-29-enterprise-agent-implementation.md`.
Оно не означает, что предлагаемые ниже observable детали уже приняты в spec.
Normative changes, hashes и AGENTS остаются у владельца основного плана.

Работа выполнена в `/private/tmp/core-agent-enterprise-work`. Реальный cached
PyPI release `croniter==6.2.4` разрешён через `uv add --offline --no-sync`, затем
`uv sync --frozen --offline` прошёл; lock не подделывался. Parser suite выполняет
настоящий package/DST adapter. PostgreSQL и browser proof отсутствуют.
`keycloak-js` не найден в доступных genuine caches, registry недоступен; UI
`typecheck` по-прежнему завершается TS2307 только на этой отсутствующей dependency.

## Реальные точки переиспользования

1. `PostgresRootAdmission._admit_transaction()` в `core_agent/admission.py`:
   root-message advisory lock → canonical chat lock → busy check →
   `create_workflow()` → `PostgresTaskStore._save(connection=...)` → receipt и
   `latest_root_run_id`. `create_workflow()` уже передаёт
   `previous_root_run_id` в `_new_workflow()`. Сейчас admission самостоятельно
   открывает transaction: cron нельзя оборачивать вокруг него второй transaction.
2. `PostgresWorkflowStore.enter_wait(connection=...)` и
   `expire_waits()`/`resolve_wait()` в `core_agent/workflow.py` — образец borrowed
   connection, повторной проверки под lock и однократного durable outcome.
   `CoreAgent._recover_workflows_once()`/`recover_workflows()` уже выполняют
   bounded coordinator pass и запускают принятые roots через recovery.
3. `owner_api.py`: `require_owner`, `read_payload`, `page_query/page_cursor` и
   registry CAS дают существующие HTTP conventions. `ToolPolicy` по умолчанию
   `require_hitl`; `CoreAgent._tool_dispatch_intent()` закрепляет approval subject.
4. `history.read_history()` сейчас идёт по admitted previous-root chain;
   root/result/transcript/input positions immutable. Skip без run не помещается
   в этот источник автоматически. Нужен additive event projection, не изменение
   terminal transcript, inbox или final result. `core_outbox` уже существует;
   cron не нужен новый dispatcher для запуска принятой Task.

## Принятые нормативные детали

- Имя инструмента `core_cron_create`; schema `{prompt, expression, timezone?}`.
  `timezone` default `Europe/Moscow`. Model не задаёт tenant, owner, context,
  task/message IDs, capability snapshot или credentials. Tool создаёт schedule
  текущего чата; owner API принимает optional `context_id`, иначе создаёт новый
  canonical owner chat без запуска Task. Один чат может иметь несколько schedules;
  chat busy authority сериализует их с обычными сообщениями.
- Dialect version1: ровно пять полей minute/hour/day/month/weekday, обычные
  числовые значения, `*`, списки, неубывающие диапазоны, положительные шаги и
  стандартные month/weekday names; Sunday 0/7, DOM/DOW OR. Отклонять seconds/year,
  macros `@...`, `H/R/L/W/#/?`, reverse ranges и невозможные даты. Ограничить
  expression 256 UTF-8 bytes и поиск следующего occurrence восемью годами;
  prompt использует существующее ограничение root input. UI показывает timezone
  и следующий UTC instant, отформатированный в этом timezone.
- DST policy version1: nonexistent wall time пропускается, ambiguous wall time
  выполняется один раз в более раннем occurrence. Эта нормативная политика реализуется adapter-ом,
  не предположением о native aware iteration croniter. Выбранный parser release должен пройти реальные
  gap/fold fixtures. Если native aware iteration не соответствует политике,
  использовать parser для календарных candidates и небольшой ZoneInfo adapter
  с UTC round-trip/fold проверкой; не писать parser выражений. Не менять DST
  semantics только потому, что offline dependency неудобна.
- Lateness: grace 60 секунд для здорового coordinator. DB time
  является authority; запоздавшие ticks за grace пропускаются. На startup и
  после прерывания scans фиксируется recovery cutoff: ещё не принятые ticks
  `<= cutoff` пропускаются независимо от grace. Первый следующий instant строго
  больше cutoff; никакого одного «компенсирующего» запуска. Долгий перерыв
  coordinator определяется также monotonic gap между завершёнными passes.
  Это точное операционное определение availability надо закрепить в spec.
- Downtime допускает одну запись интервала пропусков `{first_due_at, through}`,
  без необоснованного exact count и перебора каждого minute tick за годы.
  Reason — `service_unavailable`; overdue здорового scan — `late`; busy —
  `context_busy`. Отдельный skipped Task не создаётся.
- Disabled/deleted schedule не принимает run-now; для запуска надо включить
  disabled schedule. Run-now требует `expected_revision` и client `request_id`;
  retry того же request возвращает тот же receipt, отличающееся тело — conflict.
  Disabled/delete не отменяют уже admitted Task. Delete — tombstone, история,
  workspace, события и waits сохраняются.
- HTTP version1: `GET/POST /api/schedules`, `GET/PUT/DELETE /api/schedules/{id}`,
  `POST /api/schedules/{id}/run-now`. Create `{prompt,expression,timezone?,context_id?,request_id}`;
  PUT `{prompt,expression,timezone,enabled,expected_revision}`;
  DELETE `{expected_revision}`; run-now `{expected_revision,request_id}`.
  Public metadata: `{id,revision,context_id,prompt,expression,timezone,enabled,
  next_due_at,active_task_id}`. Tenant/owner derive from auth and canonical chat;
  foreign/deleted IDs have not-found semantics, CAS409, invalid fields400.

Parser justification: [croniter primary documentation](https://github.com/pallets-eco/croniter)
documents five-field iteration, DOM/DOW OR, timezone-aware input and bounded
`max_years_between_matches`; it also accepts extensions beyond this proposal.
Pin a tested released version, not behavior inferred from repository HEAD.
[APScheduler CronTrigger](https://apscheduler.readthedocs.io/en/3.x/modules/triggers/cron.html)
has its own wall-clock/DST behavior; introducing its job lifecycle does not solve
our transactional admission and is unnecessary here.

## Boundaries and contracts

| Files | Responsibility |
|---|---|
| `core_agent/cron_expression.py`, `cron.py`, `cron_service.py` | Calendar adapter, schedule validation/stores/occurrences и recovery/lifespan coordinator; no model executor |
| `core_agent/database.py` | Schema22 after cleanup schema21, indexes, immutable-event trigger and serving grants; rebase migration number if another schema lands first |
| `core_agent/admission.py` | Extract shared borrowed-connection root admission; canonical empty-chat creation; cron origin snapshot and automatic busy-skip hook |
| `core_agent/workflow.py`, `core_agent/runtime.py`, `core_agent/app.py` | Current-config initialization, fenced tool creation, recovery hook and shutdown wiring |
| `core_agent/owner_api.py`, `core_agent/config.py`, `core_agent/tools.py` | Owner routes; exact tool schema and capabilities in both runtime modes; existing default HITL |
| `core_agent/history.py` | Bounded immutable cron-event projection and compatible cursor reader |
| New `ui/src/Schedules.tsx`; `ui/src/App.tsx`, `types.ts`, `History.tsx` | Owner management, schedule timezone/next run, same-chat navigation and localized skip notices |
| `pyproject.toml`, `uv.lock` | Real resolved parser release and transitive dependencies |

Two tables are sufficient: `core_cron_schedules` holds current tenant-scoped
schedule revision and `next_due_at`; `core_cron_events` holds immutable version1
create/update/disable/delete/admitted/skipped facts, parameter snapshots and
idempotency receipts. Events have stable ID, `(tenant_id,schedule_id)` FK,
actor/source, schedule revision, optional task/run IDs, due instant or skipped
interval, reason, and immutable history anchor. Constraints/dedupe keys distinguish
automatic occurrence `(schedule_id,revision,due_at)` from explicit request ID.
Do not use event rows as a second execution state machine: an admitted event
points to the canonical workflow for all live/terminal state.

Store/service entrypoints (общие memory/PG):

```python
CronStore(admission, clock=None)
async create(context, payload)
get(tenant_id, schedule_id, *, connection=None)
list(tenant_id, *, limit, after=None, connection=None)
update(tenant_id, schedule_id, payload, *, actor_id)
delete(tenant_id, schedule_id, *, actor_id, expected_revision)
async run_now(context, schedule_id, payload)  # Admission
occur(context, schedule_id, expected_revision, *, connection, cutoff=None)
async occur_memory(context, schedule_id, expected_revision, *, cutoff=None)
create_from_tool(record, lease_token, call_id, arguments)
CronCoordinator.tick()  # bounded due(limit=100), existing recovery thread
next_due(expression, timezone, *, after_utc)  # strictly later UTC instant
```

`run_now` returns the ordinary admitted/failed A2A Task receipt. `CronCoordinator.tick`
обрабатывает не более100 due candidates с fair rotation; post-commit
handoff may wake existing recovery, never dispatch before commit. Every accepted
root gets immutable `cron_origin` version1 with schedule ID/revision, source
automatic/manual, due instant if automatic, and exact prompt/expression/timezone.
It does not freeze effective permissions: ordinary initialization reads current
policy, capabilities, guardrails and budgets.

## 1. Contract and parser boundary

**Files:** parent-owned normative documents/hash lock; `pyproject.toml`, `uv.lock`,
`core_agent/cron_expression.py`, `tests/test_cron_expression.py`.

- [x] Parent adopts/revises the proposals above in public contract, CRON, architecture,
  tools/config and acceptance before observable implementation. Update spec lock
  deliberately. Existing frozen authorization is sufficient; no new user question.
- [x] Resolve a mature parser release using `uv add croniter` in an environment
  with package access, review the real lock diff and run `uv sync --frozen`.
  If unavailable, record the dependency block and work only on independent storage
  boundaries. Do not edit lock entries manually or change CI to skip parser tests.
- [x] Add `CronExpressionTests` failing cases for dialect boundaries, impossible
  dates, Moscow default, invalid IANA zone, strict-after behavior and gap/fold
  dates in Europe/Berlin and Australia/Lord_Howe. Include the concrete base case:

  ```python
  def test_moscow_next_occurrence(self):
      self.assertEqual(
          next_due("0 9 * * *", "Europe/Moscow",
                   after_utc=datetime(2026, 9, 30, 5, 59, tzinfo=timezone.utc)),
          datetime(2026, 9, 30, 6, 0, tzinfo=timezone.utc),
      )
  ```

- [x] Run `python -m unittest tests.test_cron_expression -v` through
  the uv prefix below; confirm intended red, implement only `next_due` and strict
  validation with the parser, rerun green. Invalid/unbounded schedules produce
  safe `CRON_INVALID`; parser exception text is not a public response.

## 2. Company schedule storage and immutable events

**Files:** `core_agent/cron.py`, `core_agent/database.py`, `tests/test_cron.py`.

- [x] Add shared memory/PG contract cases: owner A creates, owner B in same company
  reads/edits, other company cannot read/mutate/run; bad shape/bool/NUL/surrogate
  rejected, CAS winner only, duplicate request returns same schedule, changed
  duplicate request conflicts. `context_id` owner binding is immutable.
- [x] Add migration from schema21 with nullable/backward-compatible absence of cron
  fields in existing workflows. Preserve existing tables and grants. Events are
  append-only, current schedule updates require revision CAS, and delete keeps FKs.
- [x] Implement store methods above using borrowed connections, safe error codes,
  real DB clock, bounded company cursors and indexes for enabled due rows and chat
  event anchors. Creation records an audit event; updates preserve previous values
  through their immutable event snapshot. No scheduler execution in CRUD.
- [ ] Run `python -m unittest tests.test_cron tests.test_postgres_persistence -q`.
  Under a real `TEST_DATABASE_URL`, add two-connection edit/delete races, rollback
  after event insertion, migration replay and serving-role mutation/DDL checks.

## 3. One atomic root-admission path for automatic and manual runs

**Files:** `core_agent/admission.py`, `core_agent/cron.py`, `core_agent/runtime.py`,
`tests/test_cron.py`, `tests/test_admission.py`, `tests/test_file_admission.py`.

- [x] Extract the existing PostgreSQL admission body into a connection-accepting
  helper used by both current `admit` and cron. Public A2A behavior, duplicate
  digest, file preparation outside pool transactions, busy receipts and previous
  root pinning remain covered by existing tests. Do not copy the root constructor.
- [x] Establish one order for all cron callers: explicit request/admission dedupe
  lock → schedule row → chat row → workflow/Task persistence. Update/disable/delete
  stop at schedule row. Automatic scans select candidates without retaining locks,
  then use that order per occurrence; no `FOR UPDATE SKIP LOCKED` batch followed
  by inversely ordered advisory locks. Fenced tool creation may lock its parent run
  before inserting a new schedule, but never locks an existing schedule/chat after
  holding a parent workflow lock. Validate existing chat scope read-only for tool
  creation; an existing canonical chat cannot change execution owner.
- [x] Inside admission re-read schedule revision/enabled and DB time, then chat
  busy state. Atomically write parameters/origin, Task/workflow/receipt/latest root,
  admitted event and automatic next due. Automatic busy writes only a skip event;
  manual busy writes the existing failed/CONTEXT_BUSY receipt. Explicit retries
  return stored receipts even if settings subsequently change or schedule is disabled.
- [x] Mirror atomic publication with existing memory admission locks/rollback;
  never hold a threading lock across an await. Document and test memory lock order
  against `MemoryRootAdmission.admit` and `ScopedMemoryTaskStore.admit` before adding
  any schedule lock. Do not introduce schedule→admission and admission→schedule paths.
- [x] After commit release/handoff the initial lease using existing recovery APIs;
  crash before handoff leaves the accepted workflow recoverable. No public HTTP
  loopback or fabricated user JWT; an internal trusted schedule origin binds the
  stored company/chat owner without granting the scheduled prompt owner authority.
- [ ] Add `CronAdmissionTests`: automatic/manual/simultaneous ordinary Send share
  busy authority; edit winner supplies all fields from one revision; disable winner
  admits none; admitted task survives disable/delete/restart; duplicate admission
  and crash after commit produce one root. Verify pinned previous-root history and
  same workspace, including previous compaction and negative material decisions.
- [ ] Run `python -m unittest tests.test_cron tests.test_admission tests.test_file_admission tests.test_chat_context -q`.

## 4. Durable due scan and real history notices

**Files:** `core_agent/cron.py`, `core_agent/app.py`, `core_agent/runtime.py`,
`core_agent/history.py`, `tests/test_cron.py`, `tests/test_owner_history.py`.

- [ ] Add `CronCoordinatorTests` using injected clocks: startup cutoff, failure
  between scan passes, overdue healthy scan, busy HITL/remote wait, several missed
  years without per-minute backfill, no model/tool budget use and shutdown. Compute
  cutoff from DB time before startup batch processing; repeats remain deduplicated.
- [x] Implement bounded `CronCoordinator.tick()` with `due(limit=100)` as existing recovery-loop work;
  advance cursor and commit admission/skip together. A failed transaction never
  advances next due. Rows created/edited after cutoff keep their newly computed
  future next due. Clock rollback cannot repeat an occurrence; shutdown neither
  cancels admitted roots nor synthesizes caller cancel.
- [x] Hook the cron service only after admission/store wiring is ready. Startup
  skips overdue unadmitted ticks before normal recovery launches accepted roots.
  Subsequent bounded scans use the existing coordinator lifecycle; large recovery
  backlog must not starve workflow recovery or create unbounded startup work.
- [x] Use immutable skipped event IDs as history entry IDs, anchored under the
  chat lock to the current root/transcript position, after a terminal result, or
  before the first root for an empty scheduled chat. Preserve anchor after later
  roots, transcript append, delivery, compaction and schedule deletion. Do not
  fabricate transcript sequence, mutate sealed runs or feed a skip as user input.
- [x] Adopt additive `schedule_notice` history kind and cursor v2 for mixed roots
  and notices; reader still accepts issued v1 cursors at their original positions.
  Query only bounded event pages under tenant/context/owner scope. History checks
  chat existence even with `latest_root_run_id = null`; canonical root validation
  remains unchanged. Only skip facts become chat notices; CRUD audit is not chat text.
- [ ] Verify notices and advance rollback together, repeated scans create one notice,
  foreign/corrupt cursors fail closed, a skipped tick creates zero Tasks, and reads
  do not refresh guards/acquire writer leases. Run `python -m unittest tests.test_cron tests.test_owner_history tests.test_owner_chats tests.test_wait_store tests.test_durable_waits -q`.

## 5. Owner HTTP and agent tool through existing policy

**Files:** `core_agent/owner_api.py`, `core_agent/app.py`, `core_agent/runtime.py`,
`core_agent/config.py`, `core_agent/tools.py`, new `tests/test_cron_api.py`,
new `tests/test_cron_runtime.py`.

- [x] Add ASGI cases for owner-only/no-lookup denial (external and dual-role403),
  foreign IDs404, CAS409, strict duplicate/extra JSON/query rejection, no-store,
  cursors bound to company, and shared-owner update. Add run-now duplicate and
  busy races; successful run-now returns an ordinary Task with the same context.
- [x] Wire routes to store/service, preserving exact revision and request IDs.
  Run-now never calls a provider inside the owner HTTP transaction and never
  changes automatic next due. GET computes `active_task_id` from canonical chat/run,
  rather than an independently mutable schedule flag.
- [ ] Register `core_cron_create` as mutating with `external_write` risk, available
  in both execution modes subject to capability intersection. Use default tool
  HITL without special owner bypass. Include normalized timezone and exact prompt/
  expression in approval subject; no model-controlled internal identities.
- [ ] Persist creation receipt with the parent dispatch attempt/lease so recovery
  after store commit cannot create another schedule. Use the same handler from
  Python `tools.call`; existing lifted continuation handles HITL without replaying
  the Python prefix or charging the nested tool twice. Child access requires
  explicit delegation allowlist; preserve child tenant/chat/owner bindings.
- [ ] Add default-HITL, rejected/no-side-effect, disabled/hidden/stale-call-denied,
  approved-once/recovery, Python lift, child narrowing and fresh run policy-change
  cases. UI CRUD remains usable when the tool is denied. Run `python -m unittest tests.test_cron_api tests.test_cron_runtime tests.test_interactions tests.test_python_waits tests.test_tool_approvals tests.test_auth -q`.

## 6. Owner UI and runtime acceptance

**Files:** new `ui/src/Schedules.tsx`, `ui/src/App.tsx`, `ui/src/types.ts`,
`ui/src/History.tsx`, `ui/src/styles.css`, `tests/test_ui_static.py`.

- [x] Add schedule list/create/edit/enable/disable/delete using existing `Api` and
  session lifecycle. Display expression, timezone, next local time and active state;
  preserve drafts on conflicts and refresh before an explicit retry.
- [x] Run-now disabled while chat is known busy, but always handle server busy receipt.
  Preserve one request ID for an uncertain mutation retry; no automatic mutation
  retry. Navigate to returned same chat without resetting an unrelated draft.
- [x] Render immutable notices as localized facts, never raw HTML. Deleting a
  schedule must leave its chat/history/files and existing interaction cards usable.
- [ ] Run real UI `npm ci`, `npm run typecheck`, `npm run build` once dependencies
  are available. Exercise create→approve→scheduled run→HITL overlap skip→disable→
  accepted-run completion, two-owner editing, busy run-now, downtime restart and
  timezone display in an actual browser. Static tests are not browser proof.

## Gates, migration and completion

All Python commands above use this prefix in the current worktree:

```sh
UV_CACHE_DIR=/private/tmp/core-agent-uv-cache uv run --offline --no-sync python -m unittest tests.test_cron -v
```

Replace only the module arguments with the exact groups in each task. Confirm red
for the intended regression before implementation, then green. Shared store/
admission/coordinator cases must run against memory and a real PostgreSQL database;
use inherited `unittest.skipUnless(TEST_DATABASE_URL, ...)` conventions, and report
skips separately. No new pytest framework or temporary verification harness.

- [ ] Run `uv run ruff check core_agent tests`,
  `uv run python -m unittest tests.test_spec_lock -q`, and the complete existing
  CI gate `uv run python -m unittest discover -s tests -v` after `uv sync --frozen`
  in the intended CI environment. Preserve existing failures as reported failures;
  do not weaken or remove frozen tests to obtain green.
- [ ] Execute PostgreSQL crash/competing-transaction and migration/grant tests;
  prove event/admission atomicity with injected failure before commit and recovery
  after commit. Existing accepted roots require no schedule to remain enabled.
- [ ] Schema upgrade is additive; old local/remote runs remain readable. Once cron
  roots, origin snapshots or history cursor v2 are persisted, rollback requires a
  reader supporting those versions or a coordinated DB restore. Merely deploying
  an old image can silently abandon schedules/notices and is not an allowed rollback.
- [ ] Parent updates implementation-status/release/AGENTS only with actual CI proof,
  noting dependency, live PG and browser gaps. Review logical changes, then commit
  each coherent product slice with its spec/tests; this planning task makes no commit.

**Проверенные срезы:** parser8 executed; store/calendar47 total27 executed20 PG
skips; independent coordinator9 executed3 PG skips. Independent API/composition
suite143 total75 executed68 PG skips, включая actual in-process ASGI lifespan,
recovery-thread/main-loop execution и сохранение admitted root при shutdown.
Ruff проходит. Review исправил memory lock inversions и stale readiness race;
UI сохраняет pending UUID/body при408 и abort-ит paginated reads при уходе.
Это не native PostgreSQL/browser proof и не зелёный полный release gate.

**Следующий срез:** закончить agent tool/parent fencing и его independent review,
затем выполнить combined и полный CI suite. Запустить реальные PG migration,
leader contention, rollback/unknown-COMMIT и browser acceptance при доступности
целевого окружения. Незавершённые clauses checkboxes выше остаются открытыми.
