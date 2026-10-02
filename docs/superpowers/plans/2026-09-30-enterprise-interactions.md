# Enterprise durable interactions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Выполнять три логических среза последовательно; этот документ сам не разрешает изменения frozen spec/tests.

**Goal:** Освобождать worker на durable ожиданиях, исполнять решения владельцев над неизменяемым вызовом и закрывать непроверенные материалы от agent loop до разрешения.

**Architecture:** Продолжение остаётся в существующем workflow checkpoint; небольшая таблица ожиданий хранит конкретный повод и одно окончательное решение. Существующие lease, inbox, scheduler, outbox и A2A Task обеспечивают владение, пробуждение и публичную проекцию. Текущая owner policy сужает immutable admission ceiling; UI впоследствии использует работающий owner API.

**Tech Stack:** Python 3.12, dataclasses/json/datetime, PostgreSQL/psycopg, Starlette, существующий model adapter, unittest, uv. Новых очередей, framework, CPython checkpoint engine и зависимостей нет.

## Основание, зависимости и граница

План составлен по enterprise worktree на `b7c7619`, schema 13. Auth, стабильный Principal, owner/external endpoints и atomic root admission уже существуют. `core_chats.latest_root_run_id` остаётся единственным указателем занятости: любой нетерминальный root, включая все ожидания, блокирует следующий root и удаление файлов workspace.

Разрешение пользователя на frozen spec/tests для enterprise ТЗ зафиксировано в [общем плане](2026-09-29-enterprise-agent-implementation.md). Перед исполнением применять именно его область, не трактовать этот план как новое разрешение. Нормативные источники: `spec/runtime.md` HITL-01–04/INPUT-01, `spec/tools.md` TOOL-01–02, `spec/tasks-and-delegation.md` TASK-03/LONG-03–04, `spec/agent-configuration.md` UI-02, `spec/security-and-reliability.md` GUARD-01–04 и LLM-детектор, `spec/execution-environment.md` Python CodeAct, `spec/acceptance.md` ENT-AC-02, 06–10, 17a, 21, 26–29, 36–54, 69–70.

Этот срез реализует local task wait и timer, HITL, owner questions и реальные guardrails. Remote operation handles/polling, cron и UI реализуются отдельно. `core_agent_send_message` пока остаётся существующим синхронным путём: LONG-01/02 и ENT-AC-18–20 не становятся implemented от появления общей таблицы ожиданий. [Files plan](2026-09-30-enterprise-files.md) подключает binary intake только после третьего среза и доказанной sandbox boundary; до этого enterprise binary Parts отклоняются до admission. Постоянный workspace без binary intake можно реализовывать независимо.

Migration 14 зарезервирована для waits, 15 — для owner policy/settings. Таблица material review добавляется третьим срезом следующей свободной миграцией: 16, только если этот номер к началу реализации ещё свободен. Files migration выбирает реальный следующий номер; миграции не исполняются параллельно. Во всех случаях serving image fail-closed проверяет точный SCHEMA_VERSION, schema меняет отдельная migration job после остановки старых workers. После новых записей откат требует согласованного восстановления БД; downgrade image сам по себе не поддерживается.

## Что действительно требуется изменить в текущем flow

| Точка | Сейчас | Изменение |
|---|---|---|
| `runtime.py:_continue_workflow` | Берёт lease, поднимает heartbeat, сначала потребляет inbox, затем исполняет tool queue | До восстановления execution context и inbox проверяет активное ожидание; pending возвращает `SuspendedRun`, resolved применяет сохранённую фазу |
| `runtime.py:_execute_pending` | Validation → `EXECUTING` → синхронный dispatch → `_record_tool_outcome` | Проверки/owner wait до `EXECUTING`; завершённый dispatch с непроверенным результатом сохраняется отдельно и никогда не повторяется |
| `runtime.py:_task_wait` | Блокируется в `task_scheduler.wait` | Проверяет owned Task; terminal возвращает сразу, иначе durable `WAITING_TASK` |
| `runtime.py:_delegate` | Joined delegation создаёт child, затем блокируется в scheduler.wait | Child admission и сохранение его handle в parent checkpoint атомарны; joined parent ждёт durable, не создаёт второго child при recovery |
| `postgres_tasks.py:_launch` и `tasks.py` | Любой возврат handler считается completed | `SuspendedRun` освобождает claim и thread, не вызывает `_finish` и не создаёт terminal notification |
| `runtime.py:_recover_workflows_once` | Только root `RUNNING/MODEL_RESPONDED/EXECUTING` | Дополняется sweep завершённых/просроченных ожиданий; root resume и child scheduler resume сохраняют разных владельцев execution |
| `app.py:handle/resume/result_artifact` | Любой RunResult превращается в Artifact | `SuspendedRun` передаётся без artifact conversion; tracing тоже не читает у него `message/usage` |
| `a2a_sdk.py:CoreAgentExecutor.execute` | Любой возврат handler публикуется как Artifact и complete | Suspension публикует только нетерминальное состояние и закрывает execution; подписка наблюдает durable Task |
| `database.py:reconcile_workflow_task/reconcile_from_workflows` | Проецируют только terminal workflow | Проецируют также ожидание/возобновление с committed workflow revision, без stale downgrade |
| `workflow.py:enter_approval/reserve_approval` | Остаточные methods вызывают отсутствующий runtime operator manager | Не использовать как owner authorization. Удалить после проверки всех callers; новые transitions используют существующий `_transition_locked` |

`_record_tool_outcome`, `_consume_inbound_messages`, `_consume_task_notifications`, `_record_transition`, `cancel_task`, `resume_task`, `_recover_subagent`, `_recover_background_tool`, `_python_tool_call`, `ToolRuntime.execute`, SDK Get/List/Subscribe и push — обязательные callers для ревью. Одной замены `_task_wait` недостаточно.

## Зафиксированные технические контракты

### Wait record и результат runtime

Добавить в `workflow.py` один простой immutable return type, общий для runtime и scheduler:

```python
@dataclass(frozen=True)
class SuspendedRun:
    run_id: str
    task_id: str
    wait_id: str
    workflow_version: int
```

Это не tool result и не terminal RunResult. Обрабатывается явно, до `str(result)`, artifact serialization и scheduler `_finish`. Не использовать обычный `CoreError` для штатной suspension: текущие exception paths terminalize-ят mutation.

Migration 14 добавляет `core_waits`:

```sql
CREATE TABLE core_waits (
    wait_id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES core_runs(run_id),
    tenant_id text NOT NULL,
    owner_id text NOT NULL,
    context_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    kind text NOT NULL CHECK (kind IN
        ('timer','task','tool_approval','owner_question','guardrail')),
    source_id text NOT NULL,
    subject jsonb NOT NULL,
    continuation jsonb NOT NULL,
    deadline double precision,
    outcome jsonb,
    resolved_at double precision,
    applied_at double precision,
    created_at double precision NOT NULL,
    UNIQUE (run_id, generation),
    CHECK ((outcome IS NULL) = (resolved_at IS NULL)),
    CHECK (applied_at IS NULL OR resolved_at IS NOT NULL)
);
CREATE UNIQUE INDEX core_waits_active_run_idx
    ON core_waits(run_id) WHERE applied_at IS NULL;
CREATE INDEX core_waits_due_idx ON core_waits(deadline)
    WHERE resolved_at IS NULL;
```

`subject` хранит только данные конкретного вида: timer `until`; task `task_id`; approval canonical name, origin, frozen arguments, schema digest и call ID; question свободный текст; guardrail material ID/digest/source type. Owner/private поля не попадают в public outbox payload. Файловые bytes остаются в private durable storage по files plan, не в JSONB. `continuation` содержит version=1 и одну из конкретных фаз: `tool_gate`, `tool_wait`, `tool_result`, `input`, `python_nested`; для input — inbox sequence, для tool — исходный call ID. Новых registry/plugin механик для continuation нет.

Новые store operations реализуются на обоих adapters, с одним контрактом:

```python
def enter_wait(self, record, *, kind, source_id, subject, continuation,
               deadline, snapshot, lease_token): ...
def get_wait(self, wait_id, *, tenant_id, owner_id=None): ...
def resolve_wait(self, wait_id, *, tenant_id, outcome, actor_id=None): ...
def expire_waits(self, *, limit=100): ...
```

Это сигнатуры интерфейса, не разрешение оставлять заглушки. `enter_wait` одной транзакцией блокирует run, проверяет lease/version/cancel, сохраняет snapshot и wait, переводит canonical state в `WAITING_TASK` либо `WAITING_INPUT`, пишет event/audit/outbox, освобождает lease. В snapshot хранится только текущий `wait_id`, monotonic generation и уже начисленный pending call. Повтор до commit не создаёт два ожидания. Повтор после commit возвращает прежнее по run/generation.

`resolve_wait` всегда берёт run row, затем wait row; после lock получает время PostgreSQL через `clock_timestamp()`. Если outcome уже есть, возвращает его, не пишет второй wakeup. Если deadline достигнут, выбирает timeout, даже когда owner POST пришёл раньше, но ждал lock. Cancel/terminal run закрывает wait как canceled без будущего dispatch. Иначе неизменяемое outcome + checkpoint готовности + один outbox event коммитятся вместе. `applied_at` выставляется только вместе с применением результата в checkpoint; resolved-but-unapplied запись переживает crash.

Policy transactions используют порядок locks `tool policy row → run rows в стабильном порядке → wait rows`; lease-taking runtime не держит run lock между транзакциями. Решение владельца само не выдаёт execution lease и не выполняет tool.

### Timer, local Task и восстановление

Новый `core_wait_until` имеет strict schema:

```json
{"type":"object","properties":{"until":{"type":"string","minLength":1}},"required":["until"],"additionalProperties":false}
```

Дополнительная runtime validation принимает ISO-8601 datetime только с явным UTC offset или `Z`, нормализует в UTC; naive datetime, invalid date и nonfinite timestamp дают `TOOL_ARGUMENT_INVALID`. Прошедшее время возвращает немедленный successful result. Будущее создаёт отдельную generation. Результат: `{"until":"...Z","woke_at":"...Z","reason":"time"}` либо `reason="message"` и `message_id`; результат не подтверждает наступление внешнего события.

В `append_inbound` новый, успешно inserted Message атомарно закрывает только активный timer; duplicate возвращается до wake logic. При создании timer под тем же run lock проверяется unread inbox, уже принятый после последней safe boundary: он тоже немедленно завершает новое ожидание, чтобы не потерять сообщение в промежутке до `enter_wait`. Старый timer адресуется конкретным wait ID/generation. HITL, questions, guardrails и task wait от follow-up не закрываются; inbox доставляется после завершения ожидания перед следующей моделью.

Сохранить текущую local `core_task_wait` schema и результат `_task_snapshot`: optional `timeout` — относительный срок одной local wait attempt, преобразованный при создании в абсолютный deadline. При timeout возвращается актуальный snapshot незавершённой child Task без ее отмены. Это не remote operation deadline; будущий remote handle приносит свой окончательный срок. Ownership проверяется до сохранения wait. Terminal notification и sweep конкурируют через тот же `resolve_wait`; mail acknowledgement не стирает единственный durable повод пробуждения.

Coordinator один раз за обычный recovery interval разрешает due timers и waits с terminal owned task; постоянные per-wait threads, `sleep(until)`, model polling и удерживаемые DB connections не создаются. Outbox wakeup ускоряет этот scan; correctness остаётся у persisted state. Pending wait не выбирается как исполнимый root. Resolved root запускается существующим `_launch_recovery` с fenced lease; unresolved explicit `resume_task` возвращает `SuspendedRun` без startup execution environment/MCP/model.

Child suspension освобождает и workflow lease, и scheduler claim. Scheduler Task остаётся nonterminal `working`; recovery query исключает child с unresolved workflow wait и выбирает resolved child через существующий subagent handler. Root coordinator не начинает исполнять child самостоятельно. Joined delegation сохраняет child ID и continuation атомарно с уже существующим child workflow/scheduler admission, затем parent ждёт этот ID. Crash не пересоздаёт child. Terminal child вызывает прежние mailbox/required-child semantics. Cancel рекурсивно закрывает waits всей family, отменяет children и переносит unread inbox в terminal transcript по прежнему контракту.

### Budget и dispatch

Первый model-issued call по-прежнему начисляется в `tool.attempt.started`; `pending_call` остаётся на месте всё время ожидания. Решение, timeout, restart, timer tick и notification не начисляют новых model/tool calls. Новое обращение модели и реально новый call начисляются как раньше. Истечение turns/tool budget не отключает зарезервированный finalization turn; оно закрывает неисполняемые pending calls structured budget result и не dispatch-ит одобренное действие сверх лимита. Absolute owner deadlines не становятся модельным wall-clock budget и не сбрасываются настройкой.

Policy denial, owner rejection и timeout до dispatch записываются через `_record_tool_outcome`, освобождают соответствующий pending call и продолжают loop. При разрешении проверяются сохранённая schema, текущий ceiling/deny и неизменность source/version. Только после этого фиксируется `EXECUTING` и выполняется handoff в dispatcher. Успешное решение владельца не означает, что side effect уже выполнен. Crash после intent и до известного результата сохраняет существующий `SIDE_EFFECT_UNKNOWN`, без replay.

Гонка с запретом линеаризуется в transaction handoff: проверка текущей policy revision, immutable dispatch intent и передача владения исполнителю являются одной server admission boundary. Policy change, завершившийся до неё, запрещает dispatch; уже допущенный executor call не прерывается. Не считать owner approval этим handoff. Нельзя проверять policy только при открытии карточки или до network/model wait. В нормативном тексте явно определить эту точку как передачу вызова на исполнение, сохранив отсутствие exactly-once downstream guarantee.

### Owner API и dynamic policy

Создать `core_agent/interactions.py` для owner routes, payload validation и PostgreSQL/in-memory owner settings; workflow transitions остаются в `workflow.py`. Проверка `Principal.is_owner` выполняется на каждом HTTP запросе через уже существующий middleware и повторно на decision service boundary. Principal tenant обязателен, caller-supplied owner/tenant игнорировать нельзя — такие поля отклоняются strict schema. External получает 403 до поиска ID; owner чужой компании — 404. В owner scope разрешено принять решение по external-owned Task без изменения `owner_id` этой Task.

| Method/path | Request / response |
|---|---|
| `GET /api/interactions?task_id=...&status=pending` | Owner-only список requests соответствующего scoped Task, bounded pagination, cursor; payload включает immutable subject, deadline, generation, material/call digest и сохранённый outcome |
| `POST /api/hitl/{wait_id}/decision` | `{"decision":"allow" или "reject","subject_digest":"sha256:..."}`; arguments в request запрещены |
| `POST /api/questions/{wait_id}/answer` | `{"answer":"свободный текст","subject_digest":"sha256:..."}`; непустой текст до 64 KiB UTF-8 |
| `POST /api/guardrails/{wait_id}/decision` | Такая же decision schema, но wait.kind обязан быть guardrail |
| `GET /api/tool-policies` | Текущий catalog внутри deployment ceiling + mode, guardrails_exempt, revision; новая identity возвращается как require_hitl/false |
| `PUT /api/tool-policies/{canonical_name}` | `{"mode":"allow|require_hitl|deny","guardrails_exempt":false,"expected_revision":0,"expected_origin":"builtin:..."}`; origin выбирается сервером, несовпадение precondition даёт `TOOL_IDENTITY_CONFLICT` |
| `GET /api/settings` | Три поддержанные timeout в секундах и revision |
| `PUT /api/settings` | Все три timeout и `expected_revision`; положительные целые, максимум 2 147 483 647, booleans не считаются числами |

Timeout keys: `hitl_timeout_seconds`, `owner_answer_timeout_seconds`, `guardrails_timeout_seconds`; каждый default 86 400. Изменение применяется только к новым requests. CAS revision conflict даёт 409 `SETTINGS_CONFLICT`. Decision идентичного уже принятого outcome идемпотентно возвращает сохранённое состояние; противоположное/late decision — 409 `INTERACTION_CLOSED` с safe current outcome. При истёкшем deadline сначала durable timeout, затем этот ответ. `subject_digest` mismatch — 409 `INTERACTION_VERSION_CONFLICT`. Поля неизвестной schema и неверный kind — 400; все ответы `Cache-Control: no-store`.

Migration 15: `core_owner_settings(tenant_id PRIMARY KEY, revision, hitl_timeout_seconds, owner_answer_timeout_seconds, guardrails_timeout_seconds)` и `core_tool_policies(tenant_id, canonical_name, origin, mode, guardrails_exempt, revision, PRIMARY KEY(tenant_id, canonical_name, origin))`, CHECK для mode/timeout. `origin` — runtime-derived builtin identity либо MCP server/tool identity; переиспользованный alias другого MCP tool не наследует разрешение. Policy не хранится как мутируемая часть EffectiveConfig. Перед каждым catalog build, каждым direct/nested/child/background dispatch и после approval перечитывается текущая trusted policy внутри immutable ceiling.

`deny` transaction сразу закрывает все pending tool approvals этой identity во всех чатах company, включая lifted Python и child; outcome `POLICY_DENIED`, причина policy change, не owner rejection. Уже разрешённый, но не dispatched call также отклоняется последней проверкой. `allow` не меняет существующие pending requests/deadlines. Новый tool требует HITL. Для test fixtures, которым нужен прежний automatic execution, явно задать allow через trusted test setup; не делать скрытый production bypass по модели ScriptedModel.

Новый `core_ask_owner`:

```json
{"type":"object","properties":{"question":{"type":"string","minLength":1,"maxLength":16384}},"required":["question"],"additionalProperties":false}
```

Он проходит обычную policy/HITL/guardrails для собственных arguments. После разрешения самого tool создаётся отдельный owner_question request с отдельным deadline; эти запросы нельзя слить. Успех возвращает `{"answer":"..."}` через обычный linked tool result, timeout — `OWNER_ANSWER_TIMEOUT`, rejection tool approval — `OWNER_APPROVAL_REJECTED`, timeout approval — `OWNER_APPROVAL_TIMEOUT`. Все они оставляют agent loop работоспособным. Owner answer считается недоверенным входом и проходит проверку материала до модели; обычный A2A follow-up не становится ответом.

### Python broker: continuation принадлежит agent loop

Согласованная семантика требует уточнения `spec/tools.md`, `spec/execution-environment.md`, `spec/kernel-instructions.md` и нового acceptance criterion до реализации. Произвольный CPython процесс не сериализуется. Когда nested call требует durable wait, сохраняется сам frozen nested call, процесс принудительно завершается, а после решения продолжается модель, не Python. Принятие решения разрешает только этот call, а не повтор всего Python-кода.

1. Broker получает stable request ID от RUNNER, не заменяет его новым UUID при повторе. Transaction записывает nested intent/arguments/schema digest, outer call ID, completed broker outcomes и один общий/local tool charge. Validate/current deny остаются обязательными для каждого запроса; известный synchronous denial возвращается `ToolCallError` без suspension.
2. При необходимости HITL, owner question, timer/task wait либо подозрительных args/result broker фиксирует фазу `python_nested` и запрещает последующие broker dispatch. Он не отправляет обычный catchable error, после которого Python может продолжить работу.
3. Runtime отменяет process group/namespace через существующий execution backend и получает подтверждённый teardown. Только затем одной fenced transaction переходит из outer execution в durable wait с записанными partial stdout/stderr и известными broker outcomes. Пока teardown не подтверждён, workflow не объявляется безопасно suspended.
4. Решение относится к исходным nested name/arguments/call ID и актуальной policy. Allow выполняет именно этот nested call один раз; reject/deny/timeout не выполняют. Если первоначально приостановлен уже полученный nested result, allow раскрывает сохранённый result без повторного вызова.
5. Outer `core_python_exec` завершает свой исходный call structured result `PYTHON_CONTINUATION_INTERRUPTED`, содержащим подтверждённые completed nested results, исход ожидавшего nested call и факт, что Python remainder не выполнен. Модель получает инструкции продолжать с этих фактов; prefix/remainder не запускаются автоматически. Partial filesystem/прочие эффекты до остановки не объявляются откаченными или отсутствующими.
6. Crash до подтверждённого teardown или неизвестный ранее dispatched mutating nested call сохраняет `SIDE_EFFECT_UNKNOWN`/reconciliation. Owner approve не разрешает replay неизвестного эффекта. После безопасного checkpoint restart продолжает только сохранённый nested call/результат. Если Python сам завершился, пока broker ожидал classifier, передача результата в уже отсутствующий process не предпринимается.

Не менять этот flow на «nested tools автоматически разрешены», удержание процесса 24 часа, повтор скрипта или обещание восстановления Python locals. `tools.names` учитывает policy на старте Python, а stale names повторно проверяются сервером. Tool exemption внешнего `core_python_exec` не отключает проверки nested calls.

### Guardrails и защита внешних представлений

Создать `core_agent/guardrails.py`: один bounded classifier через существующий model adapter, runtime-derived material identity и validated verdict. Модель guardrails задаётся отдельно trusted deployment: `GUARDRAILS_LLM_PROVIDER`, `GUARDRAILS_LLM_MODEL`, `GUARDRAILS_LLM_BASE_URL`, `GUARDRAILS_LLM_API_KEY`; отсутствие всего набора использует уже подключённую модель, отдельный context без tools. Частичный override запрещён. Не проксировать credentials через prompt/tool args. Все четыре имени и необходимые provider-specific ограничения задокументировать рядом с существующими LLM settings после проверки actual adapter constructor.

Detector output strict JSON: `{"verdict":"clear|suspicious|uncertain"}`. Любые tool calls, лишние поля, повреждённый/пустой ответ, timeout/provider error, unsupported/incomplete extraction дают `unverified`; arbitrary detector text не передаётся основной модели. Fixed detector instruction оценивает попытки смены инструкций/прав и не объявляет простую просьбу написать код атакой. Отдельно измерять calls/tokens/latency без raw content. `GUARDRAILS_TIMEOUT_SECONDS=60`, `GUARDRAILS_MAX_INPUT_TOKENS=100000`, `GUARDRAILS_MAX_CALLS=32` — положительные trusted per-material ceilings; сохранённые attempts считаются до physical request, restart не сбрасывает их. Без retries; исчерпание даёт owner decision, не auto-allow.

Следующая свободная migration добавляет `core_material_reviews`: tenant/run/source ID/source kind, content SHA-256, immutable private payload reference, decision (`checking`, `clear`, `pending`, `allowed`, `rejected`, `timed_out`), detector usage, wait ID, revision; UNIQUE `(tenant_id, run_id, source_id, content_digest)`. Decision привязана к точным bytes/version и не зависит от compaction. Для inline text/results payload остаётся owner-private durable data; для files используется sealed quarantine reference files service. No public raw-payload column в A2A Task. `pending` создаёт guardrail wait; detector failure помечается «Не удалось проверить», не «обнаружена атака».

Проверяются initial Message до initial context/model initialization, follow-up до consume-inbound/context append, owner answer, tool args до dispatch и результат до `_record_tool_outcome`/stream/context. Guarded result сначала сохраняется как завершённый dispatch с private result reference; при crash он не остаётся ambiguous `EXECUTING` и не redispatch-ится. Phase `tool_result` раскрывает или заменяет его linked structured result. Raw remote frames из `remote_agents.py` не обходят эту границу.

Полный файл проверяется extracted chunks с overlap в рамках документа; chunk size рассчитывается существующим token counter и контекстным budget detector. Только успешная проверка всех chunks даёт clear. Неизвлечённые части, неподдерживаемый binary, хвост за лимитом, ошибки parser или достигнутый budget дают unverified и owner wait. Не проверять лишь summary/первые токены. Files service предоставляет complete extraction manifest либо явный incomplete/unsupported result; classifier не обещает парсинг всех форматов.

До clear/owner allow quarantine bytes физически вне workspace/sandbox mounts, retrieval, memory и model-facing tools. Reject/timeout сохраняют immutable denied version; модель получает только source/type/code, без цитаты содержимого. Другое чтение того же source/version, summary/retrieval или смена tool name не открывают его. Одобренный file material позволяет files service выполнить собственный atomic publication barrier; decision commit сам не делает файловый rename или partial publication. Проверка guards не обходится наличием уже сохранённого attachment receipt. Owner history и accepted quarantine не удаляются orphan-upload sweeper.

`guardrails_exempt=true` применяется отдельно к args/result именно этой identity и не вызывает detector. Оно не отменяет HITL/deny/schema/tenant/budget, не проверяет входящие message/file автоматически и не переоткрывает уже rejected/timed_out review. Установить его можно только owner API, не MCP annotations. Изменение во время pending guardrail не создаёт автоматическое owner allow: уже созданный запрос сохраняет deadline; subsequent calls используют новую настройку.

Внешнему caller доступны generic waiting и итоговый ответ. Для external-owned Task публичный stream и persisted A2A payload получают только безопасные статусы и предназначенный caller-у final Artifact: никаких tool-call arguments/results, owner question/answer, detector payload, private transcript/summary и ADK reasoning. Это whitelist publication, а не фильтрация по ключам после утечки. `TaskStreamPublisher.tool_call` сейчас вызывается до dispatch: этот путь нужно закрыть до подключения `core_ask_owner`. Owner читает внутреннюю переписку через owner API; её не вставляют в общую SDK Task history даже при owner подписке. GetTask/ListTasks/Subscribe, current snapshot, recovery projection, push и artifact/file metadata используют одну public shape. Финальная модель использует сведения для ответа, но получает kernel instruction не копировать внутреннюю переписку автоматически.

## Три reviewable commits

### 1. Durable timer/local wait и suspension без ложного завершения

**Files:** `core_agent/workflow.py`, `database.py`, `runtime.py`, `tasks.py`, `postgres_tasks.py`, `a2a.py`, `a2a_sdk.py`, `app.py`, `config.py`, `kernel.py`; `tests/test_runtime_observability.py`, `test_tasks_tools_execution.py`, `test_postgres_persistence.py`, `test_admission.py`, `test_a2a_config.py`; соответствующие разрешённые spec, `tests/test_spec_lock.py`, `AGENTS.md`.

- [ ] Сначала зафиксировать schema 14, strict `core_wait_until`, SuspendedRun и local-vs-remote timeout semantics в `spec/architecture.md`, `tools.md`, `runtime.md`, `tasks-and-delegation.md`; добавить criterion child suspension/charged-once/recovery. Обновить frozen hash lock существующим способом, не ослаблять его.
- [ ] Добавить failing runtime test по примеру `tests/test_runtime_observability.py:RuntimeTests` и PostgreSQL test по примеру `PostgresRestartTests.test_workflow_lease_outbox_and_background_recovery`. Создание простого record — существующий `WorkflowRecord(...)`, не новый harness. Точный store-level случай:

```python
wait = store.enter_wait(record, kind="timer", source_id="call-1",
    subject={"until": "2026-10-01T15:00:00Z"},
    continuation={"version": 1, "phase": "tool_wait", "call_id": "call-1"},
    deadline=1790866800.0, snapshot=record.snapshot, lease_token=token)
first = store.resolve_wait(wait.wait_id, tenant_id=record.tenant_id,
    outcome={"reason": "message", "message_id": "m-1"})
again = store.resolve_wait(wait.wait_id, tenant_id=record.tenant_id,
    outcome={"reason": "time"})
self.assertEqual(first.outcome, again.outcome)
```

  `enter_wait` возвращает stored wait record с `wait_id/outcome`; testing clock установить до deadline. В production outcome `message` доступен только transactional `append_inbound`, не public API. Для реальной гонки запускать competing transactions через существующие thread barriers/`_wait_for_blocked_query`, не последовательные вызовы.
- [ ] Реализовать таблицу и store operations выше, memory adapter под существующим RLock, все timestamps PostgreSQL после row lock. Атомарно связать inbox insert/timer wake. При terminalization пометить незавершённое ожидание canceled вместе с unread dispositions.
- [ ] Подключить early suspension/resolved phases к loop; заменить local `_task_wait` и joined delegation, обновить оба scheduler adapters. Обязательно подтвердить отсутствие живых task threads/lease/claim после suspension, required-child nonterminal state и ровно один child admission.
- [ ] Обновить app conversion/tracing, SDK execute/restore, public projection и recovery. Timer/local task остаются A2A `working`; owner interaction позднее будет `input-required` только в owner view, external generic waiting остаётся `working`. Повторная подписка пассивна. Новые handlers в registry/kernel/config доступны внутри mode ceiling и delegation allowlist.
- [ ] Проверить: future/past/invalid datetime; duplicate/stale generation; follow-up до/после wait commit; crash после resolve до apply; two workers one lease; child/grandchild wait; joined-parent cancel; shutdown до/после admission; сутки ожидания без model/tool charges; post-wait root `CONTEXT_BUSY` до terminal.
- [ ] Выполнить targeted и общие gates ниже; commit `feat: resume durable timer and task waits without holding workers`.

### 2. Owner policy, HITL/questions и безопасная остановка Python

**Files:** новый `core_agent/interactions.py`; изменения `workflow.py`, `database.py`, `runtime.py`, `python_exec.py`, `execution.py`, `tools.py`, `config.py`, `app.py`, `kernel.py`, `a2a_sdk.py`, `push.py`; новый `tests/test_interactions.py`; существующие `tests/test_auth.py`, `test_keycloak_integration.py`, `test_python_exec.py`, `test_postgres_persistence.py`, `test_admission.py`; разрешённые normative files/hash lock, `.env.example`, `README.md`, `AGENTS.md`.

- [ ] Зафиксировать owner routes, strict payloads, codes, default policy/settings, dispatch handoff и Python lift semantics в spec до кода. Private/public behavior проверить по INPUT-01/ENT-AC-47. Добавить отдельные acceptance criteria для Python partial completion и unknown mutation; не объявлять checkpoint CPython.
- [ ] Создать `InteractionTests(AuthAppTestCase)` и PostgreSQL subclass по `AuthAdmissionTests/PostgresAuthAdmissionTests`; сохранить existing real Keycloak gate. Новые tests задают scripted `core_ask_owner`/protected tool response, через `self.http` вызывают owner API, затем проверяют настоящий workflow/tool counter. Один конкретный HTTP negative case:

```python
response = await self.http.post(
    "/api/hitl/not-owned/decision", headers=self.headers("external-a"),
    json={"decision": "allow", "subject_digest": "sha256:00"})
self.assertEqual(response.status_code, 403)
self.assertEqual(self.app.state.core_agent.tool_runtime.execution_count, 0)
```

- [ ] Реализовать migration 15/settings/policy routes и собственно owner decisions через wait store. Проверить allow/deny/reject/timeout race, duplicate decision, changed args/digest, cross-company IDs, external dual-role, token revocation и owner B acting on external A task без смены owner.
- [ ] Включить default HITL в catalog/dispatch для каждого tool, включая nested/child/background. Все существующие automatic-execution tests явно задают policy своего сценария; никаких allow-by-default production settings. `core_ask_owner` получает отдельный вопрос после прохождения собственной tool policy. Режим `deny` закрывает pending во всех company chats в одной policy transaction.
- [ ] Реализовать Python lift по шести шагам выше через existing execution teardown. Broker source ID, nested charge, durable journal и known results не теряются; catch-all broker handler не превращает suspension в обычный catchable ToolCallError. Неподтверждённый teardown не допускает safe wait.
- [ ] До tool регистрации включить public publication whitelist и private owner list/history. Scripted sentinel `OWNER_PRIVATE_SENTINEL` в question/answer искать во всех external Get/List/stream/push/artifact/metadata response bytes, до и после реального restart; final scripted ответ использует безопасную формулировку. Owner endpoint должен сохранить исходные тексты.
- [ ] Python tests реально выполняют процесс: prefix пишет marker один раз, nested protected call требует HITL, remainder пишет другой marker. После allow nested dispatch count=1, prefix count=1, remainder отсутствует, модель получает interrupted result; после restart те же свойства. Дополнительно nested deny во время wait, result wait после уже выполненного side effect, concurrent owner decisions, process exit/crash во время teardown и неизвестная mutation без replay.
- [ ] Выполнить targeted/full/PostgreSQL/Keycloak gates; commit `feat: enforce owner tool policy and durable private decisions`.

### 3. Реальные guardrails и quarantine decision boundary

**Files:** новый `core_agent/guardrails.py`; `workflow.py`, `database.py`, `runtime.py`, `model.py`, `interactions.py`, `app.py`, `remote_agents.py`, `kernel.py`; интеграция с фактически реализованным private file storage из [files plan](2026-09-30-enterprise-files.md); новый `tests/test_guardrails.py`, существующие `test_python_exec.py`, `test_postgres_persistence.py`, `test_transfer_features.py`, `test_auth.py`; spec/status/hash lock, `.env.example`, `README.md`, `AGENTS.md`.

- [ ] Уточнить material schema/versioned refusal, settings/detector contract и completed-result continuation в spec. Присвоить material migration реальный свободный номер после согласования с files work; обновить runtime role grants. Использовать existing ModelResponse/ScriptedModel как classifier fixture, не новый framework или benchmark.
- [ ] Написать failing tests для всех verdict/error вариантов. Classifier fake возвращает `ModelResponse(message='{"verdict":"suspicious"}')`; assert основной scripted model ещё не вызван, private source отсутствует в context/workspace, owner видит request, wait deadline сохранён. После reject/timeout модель получает linked safe notice и может закончить Task.
- [ ] Реализовать bounded classifier и durable material review. До основного model call проверить initial/follow-up/owner answer; до tool dispatch args; после физического call сохранить private result и проверить его до tool-result stream/context. Подключить Python result lift и child flow к той же функции. Считать attempts до сети; interrupted detector attempt после restart считается израсходованной.
- [ ] Подключить quarantine release к реальному files publication barrier; complete extraction/chunk coverage обязательны для clear. Empty/partial/unsupported, tail beyond budget, tool-call-emitting detector и disabled provider создают unverified owner request. Ни временный allow stub, ни test-only detector не открывают binary intake production.
- [ ] Проверить исключения identity: exempts args/results без detector, но не inbound/file и не nested tool. Установленный deny сильнее allow/exempt. Reject/timeout сохраняются после exemption change, compaction/retrieval/memory и повторного file read. Неизвестная metadata MCP не создаёт exemption.
- [ ] В PostgreSQL гонять decision vs deadline vs cancel, crash после physical tool result до review, restart с непроверенным file, два источника одной Task и approved/rejected соседние файлы. Assert нет replay completed call, нет утечки rejected payload и нет partial публикации входящего набора. Files intake остаётся закрытым, пока его собственный atomicity gate не пройден.
- [ ] Выполнить targeted/full/PostgreSQL и применимые file/sandbox gates; commit `feat: gate untrusted materials through durable owner review`.

## Проверки и критерий завершения

Существующие примеры тестов: `tests/test_auth.py:AuthAppTestCase` для HTTP/introspection, `tests/test_admission.py:AuthAdmissionTests` для работающего root/follow-up, `tests/test_postgres_persistence.py:PostgresRestartTests` для database locks/restart, `tests/test_python_exec.py:PythonExecTests` для настоящего broker process. В `tests/test_runtime_observability.py` уже есть lease/recovery/shared-budget tests; сохранить их guarantees. Fixtures не заменяют PostgreSQL race proof.

Для каждого commit сначала его targeted modules, затем обязательные gates. Нужны реальные `TEST_DATABASE_URL`, `TEST_KEYCLOAK_URL`, `TEST_KEYCLOAK_ADMIN`, `TEST_KEYCLOAK_ADMIN_PASSWORD`; значения берутся из существующей test environment, не пишутся в Git.

```bash
uv run python -m unittest tests.test_runtime_observability tests.test_tasks_tools_execution tests.test_postgres_persistence tests.test_admission tests.test_a2a_config -v
uv run python -m unittest tests.test_interactions tests.test_python_exec tests.test_auth tests.test_keycloak_integration -v
uv run python -m unittest tests.test_guardrails tests.test_transfer_features -v
uv run ruff check core_agent tests
uv run python -m unittest discover -s tests -v
uv run python -m unittest tests.test_spec_quality tests.test_spec_lock -v
git diff --check
```

Новые modules запускать после их создания в соответствующем срезе. Exit status 0 и отсутствие skipped обязательных PostgreSQL/Keycloak cases — условие зачёта, не количество тестов. Нормативные status entries помечаются implemented только для прошедшего обычный CI proof. ENT remote/cron/UI и binary files acceptance не закрывать косвенно; AGENTS обновлять только фактами уже поставленного среза.

Проверка самого этого документа: его relative links/названия существующих entrypoints и `git diff --check`; документ не создаёт runtime capability, не изменяет frozen bytes и не требует запуска production services.

## Промежуточная проверка direct model waits — 30 сентября 2026

Schema 14 и store operations подключены к direct model `core_wait_until`, local
`core_task_wait` и joined child admission. Оба scheduler освобождают worker/claim;
A2A сохраняет working, cancellation читает canonical result, stale SDK snapshots
и chunk aggregates защищены от изменения. Spec review выявил и помог исправить
повторный child admission старым worker и starvation после первых 100 waits;
добавлены воспроизводящие regression tests. Quality review завершён после
исправления scheduler initial-claim race и post-commit worker launch failure.

Targeted memory/ASGI/SDK проверки проходят. Общий запуск до последних review fixes:
526 tests, 40 errors socket permission, один environmental assertion и одна
регрессия начального SUBMITTED projection; последняя исправлена. PostgreSQL,
настоящий Keycloak и Linux sandbox gates в текущей сессии недоступны. Добавлен
PostgreSQL runtime test с новым pool и прежними parent/child wait IDs; live proof
не заявляется. Python nested suspension относится к следующему разделу и ещё
не подключён. Release gate и весь этап не отмечены завершёнными.

## Промежуточная проверка owner control plane — 30 сентября 2026

Schema 15, memory/PostgreSQL settings и per-origin policies подключены до
recovery. `owner_api.py` содержит owner-only routes, строгие payloads, CAS,
неизменяемый digest, ограниченную пагинацию root family и идемпотентные решения.
Истечение deadline коммитится до ответа о конфликте. Store и API прошли
отдельные reviews; PostgreSQL cases остаются непроверенными в этой среде.

Direct model HITL, вопросы владельцам, current deny и приватный external stream
подключены. Сквозной ASGI proof проводит внешний запрос через approval, private
question/answer и follow-up к публичному final Artifact. Mixed text+tool response
больше не становится старым final после ожидания. Python классифицируется как
потенциально mutating: неизвестный исход требует reconciliation.

Исправлены обе проблемы первого runtime review: schema/availability после MCP
reconnect проверяются по live catalog внутри immutable ceiling; nested intent
сохраняется вместе с charge под lock актуальной policy. Background target имеет
собственный durable workflow/HITL. Сохранённый outcome child имеет приоритет над
поздней отменой в memory/PostgreSQL scheduler, включая ошибку и mailbox result.

Второй review выявил и помог закрыть ложное начисление background usage при
отказе общего budget, повторное начисление после сохранённого отказа и перехват
Python-ом nested unknown mutation/budget exhaustion. Неизвестная мутация теперь
durable завершает run как ABORTED; исчерпание budget сохраняется до broker reply
и требует финализации без tools. Terminal, как Python, всегда mutating независимо
от owner policy. Известный command exit failure остаётся catchable tool error.

Owner API/store/approval/waits/spec gate: 97 tests, 67 passed, 30 PostgreSQL
skipped. Scheduler/wait/task gates: 72 tests, 54 passed, 18 PostgreSQL skipped.
Полный suite после этих изменений: 622 tests, 442 passed, 139 skipped, 40
socket-permission errors и один assertion о причине заблокированного соединения.
Это не зелёный release gate. Позднее добавленные focused regressions проверяются
отдельно; не отмечать ENT HITL implemented на основании unit tests.

### Уточнение следующего среза: Python и background

- Nested Python intent использует существующий fenced workflow transition:
  trusted outer lease, отдельный runtime attempt ID, exact arguments/origin/schema,
  shared charge и local usage сохраняются вместе. Callback broker работает в
  другом thread; outer handler после его возврата обязан перечитать snapshot и
  version, а не затереть вложенную запись старой копией.
- Перед durable wait сохраняется `python_execution.phase=stopping`; broker
  прекращает новые calls и удерживает ответ. Catchable exception/EOF не является
  остановкой, потому что пользовательский код может продолжиться. Только
  подтверждённый `SandboxProcess.stop()` с завершённым деревом позволяет записать
  `stopped`, partial output и continuation `python_nested`, затем освободить lease.
  После resume исполняется только frozen nested call, внешний Python получает
  interrupted result; prefix и remainder не исполняются вновь.
- `SandboxLauncher` подключён к execution lifecycle; cleanup marker ставится
  после подтверждения остановки namespace. Его
  `broker_socket` безопасно монтирует один socket в
  `/run/core-agent/broker.sock`; новый IPC protocol не требуется. Socketpair можно
  использовать для isolated unit проверки существующего broker handler, но это
  не подменяет Linux bind/mount/namespace acceptance.
- Background target получает минимальный child workflow без model/finalizer
  reserve. Existing scheduler admission callback атомарно сохраняет его,
  scheduler row и stable handle в parent. Target wait освобождает claim, как
  обычный child; policy check и target intent используют ту же транзакцию.
  Result сохраняется до scheduler finish; recovery читает outcome либо
  reconciliation, не повторяет dispatched mutation. Старые contracts сохраняют
  прежнюю conservative recovery семантику.

### Python stop/lift: реализованный runtime path

Вложенный вызов сохраняется до остановки процесса, safe wait освобождает lease,
resume выполняет frozen call и возвращает interrupted outer result без повторения
prefix/remainder. Проверены allow/reject/timeout/deny, изменение schema, cancel,
сохранённые outcomes, owner question, timer, task wait и joined child. Копия
trusted context передаётся broker thread. Ошибка создания broker до запуска
процесса маркируется `ExecutionNotStarted`, сохраняя обычный tool failure.

Review нашёл и устранил неправильный continuation для joined child и отсутствие
конечного HITL timeout при переключении allow→require_hitl во время остановки.
Совместный gate Python/approvals/manager/sandbox: 85 tests, exit 0. Native probe
добавлен в Linux acceptance, но здесь не выполнялся. Общий enterprise gate и
подключение guardrails остаются незавершёнными.

### Guardrails: classifier foundation

`core_agent/guardrails.py` проверяет строгий verdict, все фрагменты полных
извлечённых документов с перекрытием, суммарный token/call budget и deadline.
До обращения к модели требуется callback durable attempt; его ошибка прерывает
проверку до сети. Timeout возвращает unverified, поздний ответ игнорируется,
единственный in-flight slot ограничивает количество незавершённых запросов.
Результат содержит только verdict/reason и usage, без исходного текста или
произвольного ответа детектора. Отдельные тесты проверяют механику, а не точность
классификации на данных компании.

Material store и production model configuration подключены в composition root;
input/result gates прошли отдельную интеграцию и review ниже. Связь с file
quarantine ещё не подключена. Наличие classifier не меняет binary admission
и не является выполнением всего enterprise guardrails acceptance.

Проверка classifier: 7 tests проходят; spec/code review устранён случай допуска
ответа без `finish_reason`, допустимы только явные `stop`/`end_turn`. При создании
production adapter нельзя наследовать произвольный `extra_body`. Общий adapter
теперь удаляет из него `tools`, `tool_choice`, legacy `functions`/`function_call`,
чтобы пустой runtime catalog нельзя было обойти provider options.

Точки следующей runtime интеграции после private file batches:

- `_new_workflow` сохраняет исходный prompt privately; до `_initialize_workflow`
  и compaction нужно разрешить initial input review. Resolved input wait обрабатывается
  раньше initialization, включая child admission.
- `_consume_inbound_messages` проверяет источники по message ID/sequence перед
  append/consume. Его terminal-only callers только disposition-ят unread input
  и никогда не запускают classifier или новое ожидание.
- `_execute_pending` проверяет arguments после schema/ceiling/deny и до dispatch
  intent; сетевой classifier не выполняется под policy transaction.
- `_record_tool_outcome` получает full private completed-result reference до
  stream/context. Сохранённый известный результат переводит continuation в
  replay-safe фазу, а после review раскрывается или заменяется safe linked result.
  Background resume обрабатывает эту фазу до `_execute_pending` и terminalization.
- `_apply_tool_wait` сначала атомарно применяет прежний wait и сохраняет его
  результат; лишь затем может открыть guardrail wait. Owner answer проверяется
  независимо от исключения для самого `core_ask_owner`.
- `_python_tool_call`, `_admit_nested_dispatch`, `_complete_python_nested`
  проверяют full arguments/results до broker reply и truncation. Result review
  сохраняет completed reference и использует stop/lift с phase `tool_result`;
  nested request ID обеспечивает стабильную identity, исходный вызов не повторяется.
- `_consume_task_notifications` не добавляет сырые payloads в контекст в обход
  review. `_relay_remote_stream` не пересылает непроверенные raw frames.
  `_child_agent` разделяет material store и один bounded classifier с родителем.
- Guardrail wait resolution обрабатывается отдельно от tool approval;
  существующие `input`, `tool_gate`, `tool_result`, `python_nested` continuation
  phases достаточны. Private references не попадают в SDK history/metadata.

### Guardrails: durable material store

`material_reviews.py` и schema 17 сохраняют exact source/digest, immutable private
payload либо sealed file reference, frozen limits/deadline, charged attempts,
observed usage и decision. Scope включает tenant/run/owner/context/task без
wildcard для пустого owner. Provider I/O выполняется вне store transaction,
после durable charge; interrupted attempt не повторяется автоматически.

Pending material и существующий guardrail wait создаются атомарно; storage error
откатывает также workflow/wait/lease. Runtime-read требует live lease и clear/allow,
owner-read доступен через owner-only HTTP route, отдельно от runtime lease API.
Решение синхронизируется с authoritative wait; поздний clear, reject/timeout и
смена deadline не открывают закрытый материал. Frozen per-call classifier caps
не меняют shared in-flight slot; budget/deadline race сохраняет уже наблюдённые расходы.

Focused classifier/material gate после исправлений: 52 tests, exit 0,
22 PostgreSQL skips. Реальные PostgreSQL proofs не выполнены этим срезом.

### Guardrails: composition и owner material API

Composition root создаёт paired classifier/material store перед recovery;
PostgreSQL deployment использует тот же database/workflow, memory deployment —
тот же memory workflow. Connection overrides и positive bounded limits
проверяются при старте. Нет production auto-clear или disable setting.

`GET /api/guardrails/{wait_id}/material` требует owner role, определяет scope
по principal и сохранённому wait, возвращает private payload либо sealed file
reference с `Cache-Control: no-store`. External caller не получает ни материал,
ни возможность отправить решение. Query overrides отклоняются.

Focused configuration/owner API/spec gate: 14 tests, exit 0. В него входит
реальный external A2A input → suspicious verdict → owner material read/reject →
cache reset → resume без rejected prompt в контексте модели → completed Task.
Синтетические detector verdicts проверяют управление выполнением, не точность
распознавания атак. Native/PG gates ещё требуются.

### Guardrails: runtime integration

Initial input проверяется до initialization/MCP/model context. Follow-up доставляется
по порядку после проверки; terminal disposition не запускает новый detector/wait.
Tool args проходят schema/deny/HITL и review до dispatch. Completed result
сохраняется отдельно до review; обычный, background и nested Python resume
используют сохранённый outcome без повторного физического вызова. Owner answer
проверяется независимо от exemption самого question tool.

Nested Python при pending material подтверждает teardown до открытия wait;
ошибки lease/storage не возвращаются интерпретатору как catchable исключение.
На recovery после crash между сохранением результата и review выполняется только
lift, не prefix/remainder. Deadline race clear→unverified использует ту же
проверку остановки. Raw tool calls/results, reasoning и remote progress не
публикуются в SDK stream; проверенный финальный ответ и lifecycle сохраняются.

Exact material digest в private review metadata отделён от call/source identity.
Отказ/timeout авторитетного wait проверяется в том же tenant/owner/context до
новой классификации или exemption, включая другой tool и следующий run.
Новые bytes/version могут проверяться отдельно. Семантическое сравнение разных
кодировок/перефразировок и качество детектора этим не доказываются.

Focused runtime/Python/HITL/classifier/material gate: 121 tests, exit 0,
98 выполнены, 23 PostgreSQL skips. Independent spec/code review принят после
исправления deadline race, nested error exposure, lease/storage catchability
и cross-call rejected-material bypass. Owner stream privacy дополнительно
проверена через реальный ASGI transport. Socket-based streaming fixtures обновлены
под private projection и проверяют разрешённый result в следующем model request;
их HTTP listener здесь недоступен.

Общий прогон на промежуточном снимке: 782 tests, 6 failures, 34 errors,
182 skips. Ошибки socket listener/AF_UNIX и network cause assertion связаны с
текущей средой; suite не объявляется зелёным. File extraction/quarantine,
реальный PostgreSQL и Linux sandbox proofs остаются отдельными обязательствами.
