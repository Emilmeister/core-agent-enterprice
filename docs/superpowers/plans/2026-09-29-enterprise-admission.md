# Enterprise chat admission — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Атомарно принимать одну корневую задачу на чат, возвращать сохранённый CONTEXT_BUSY и дедуплицировать создание по authenticated caller/messageId.

**Architecture:** Admission перед SDK execution; PostgreSQL transaction объединяет chat, request ledger, A2A Task и существующий workflow/checkpoint/budget/outbox. Состояние занятости выводится из последнего root workflow; отдельной task state machine нет. Память остаётся адаптером разработки.

**Tech Stack:** Существующие Python, Starlette/A2A SDK, psycopg, unittest, uv; новых dependencies нет.

## Граница подэтапа

Подключить admission к авторизованным enterprise endpoints. Legacy development
сохраняет прежний adapter; внутренние child admissions не занимают root slot.
Бинарные Parts enterprise-входа пока отклоняются до любых эффектов:
старый artifact backend не даёт atomic file publication. Следующий подэтап
workspace/files подключает staging к этой же транзакции; ENT-AC-11/63/66 до
этого не объявляются полностью implemented. Текстовые и data Parts принимаются.

## Контракт и схема

- `core_chats`: PK `(tenant_id, context_id)`, immutable `owner_id`,
  `latest_root_run_id` nullable FK core_runs, `schema_version=1`, created_at.
- `core_root_messages`: PK `(tenant_id, actor_id, message_id)`, request_digest,
  fingerprint_version=1, owner_id, context_id, task_id, schema_version=1,
  created_at. Ledger и Task хранятся без автоматического TTL.
- Digest — SHA-256 deterministic сериализации исходного A2A Message до SDK
  генерации IDs; отсутствие contextId сохраняется как отсутствие. HTTP token,
  trace headers и параметры формы ответа не входят в digest.
- Stable caller — проверенный Principal.actor_id. Execution owner — owner чата;
  два владельца имеют общий execution scope и отдельные dedup keys.
- Начальный Message сохраняется в Task.history при commit, включая назначенные
  task/context IDs. SDK может повторно append-ить его; persistence adapter
  устраняет только одинаковые user Message IDs в одной Task.
- `CONTEXT_BUSY`: failed Task, metadata.error с code и activeTaskId, понятный
  status.message; новый UUID, без workflow/worker/budget reservation.
- Изменённый Message при том же ключе: InvalidParamsError с
  data.code=`MESSAGE_ID_CONFLICT`; прежняя Task остаётся неизменной.
- Пустой messageId или не-ROLE_USER отклоняется до записи.
- Чужой context даёт TaskNotFoundError без owner/activeTaskId. Неизвестный context
  можно создать. Context, встречающийся только в legacy Task/workflow, без
  явного migration mapping не присваивается новому caller.

Schema 13 добавляет таблицы/indexes/grants без изменения legacy rows/blobs.
Serving process продолжает только проверять version в production. Сначала
остановить старые admission workers, выполнить отдельную migration job, затем
запустить image schema 13. После новых ledger/chat writes откат image требует
согласованного DB backup/reverse migration; просто запускать schema 12 нельзя.

## Файлы и выполнение

### 1. Зафиксировать контракт и воспроизвести отсутствие admission

- [x] Дополнить `spec/architecture.md` конкретной schema 13 и совместимостью,
  `spec/public-contract.md` error shape/fingerprint. Обновить lock hashes.
- [x] В `tests/test_auth.py` выделить только общий fixture `AuthAppTestCase`,
  сохранив все существующие assertions. Создать `tests/test_admission.py`:
  AuthAdmissionTests и PostgreSQL subclass используют тот же HTTP/Keycloak stub.
- [x] Добавить failing tests: два новых root в одном blocked chat дают один
  model call и отдельный failed Task; тот же messageId возвращает исходный ID;
  изменённый prompt/context даёт конфликт; external B не захватывает context A.

```python
first = await send("m1", "chat", return_immediately=True)
busy = await send("m2", "chat", return_immediately=True)
assert first["id"] != busy["id"]
assert busy["status"]["state"] == "TASK_STATE_FAILED"
assert busy["metadata"]["error"]["code"] == "CONTEXT_BUSY"
assert (await send("m2", "chat"))["id"] == busy["id"]
```

Run `uv run python -m unittest tests.test_admission -v` с TEST_DATABASE_URL.
Начальная ошибка должна показывать отсутствие busy/dedup, а не импорт/fixture.

### 2. Реализовать одну точку admission

- [x] Создать `core_agent/admission.py`: конкретные memory/PostgreSQL adapters,
  canonical fingerprint, Task construction и scope checks. Не вводить framework
  transactions или абстрактный storage protocol ради двух локальных реализаций.
- [x] В `core_agent/database.py` добавить migration 13, grants и возможность
  `PostgresTaskStore._save` использовать переданное connection.
- [x] PostgreSQL lock order: advisory xact lock для dedup key, затем chat row.
  Сначала duplicate lookup, затем INSERT ON CONFLICT/SELECT FOR UPDATE chat.
  Проверить owner после lock; latest_root state читать без FOR UPDATE.
- [x] Если latest root нетерминален, сохранить busy Task и ledger. Иначе через
  `_new_workflow(connection=..., defer_initialization=True)` записать root,
  initial lease/finalization reserve; сохранить Task/history, ledger и latest root.
  Исключение откатывает весь набор. Никакой сети или model/tool до commit.
- [x] Не очищать chat pointer в terminal transitions: canonical terminal state
  уже освобождает слот, а latest_root_run_id остаётся исторической ссылкой.
  Это исключает новые hooks и встречный lock order во всех terminal paths.
- [x] Memory adapter сериализует admission общим lock; duplicate/busy не
  создают новый workflow. Все данные остаются scoped теми же ключами.

### 3. Подключить SDK и существующий runtime

- [x] `core_agent/app.py`: создать admission adapter при auth_settings,
  передать callback в build_starlette_app; один настоящий memory TaskStore
  разделяется callback и SDK. Для accepted root сохранить lease в trusted state.
- [x] `core_agent/a2a_sdk.py`: общий admission вызывается в send и stream после
  проверки follow-up branch. Duplicate/busy сразу возвращает Task; accepted
  присваивает IDs params.message и идёт прямо в super без повторного admission.
- [x] Stream первым возвращает persisted Task, затем SDK events. Отсоединение
  клиента не отменяет принятый workflow; recovery запускает его после lease expiry.
- [x] Если SDK setup падает до executor enqueue, освободить только initial lease
  этого запроса. Не освобождать lease из finally уже работающего stream.
- [x] `core_agent/runtime.py`: принять optional initial lease для уже admitted
  workflow. Транзакционный `_new_workflow` не публикует локальный scope/log до
  commit; `_initialize_workflow` и recovery уже восстанавливают scope.
- [x] Сохранённый initial Message не попадает в историю дважды при SDK update,
  а recovery до первого SDK event не теряет initial history.

### 4. Проверить гонки, restart и compatibility

- [x] Два конкурентных caller запроса одного чата; конкурентные duplicates через
  независимые DB connections; разные чаты не блокируют друг друга.
- [x] Duplicate completed и busy после нового app instance с тем же PostgreSQL;
  смена token не меняет dedup. Два owners с одинаковым messageId независимы.
- [x] Owner в external chat сохраняет его execution owner; external B получает
  not-found. Legacy context не захватывается автоматически.
- [x] Нетерминальный wait удерживает слот; terminal workflow позволяет новый root;
  child workflow не занимает дополнительный слот.
- [x] Ошибка внутри admission откатывает Task, ledger, workflow, budget и outbox.
  Crash после commit до SDK start восстанавливает тот же root с одной history.
- [x] REST и JSON-RPC send/stream, returnImmediately, immediate follow-up/cancel;
  обычные legacy tests остаются зелёными.
- [x] Обновить AGENTS.md, README и implementation-status только по реально
  доказанному scope. Зафиксировать один проверенный product commit.

```bash
uv run python -m unittest tests.test_admission tests.test_auth -v
uv run ruff check core_agent tests
uv run python -m unittest discover -s tests -v
uv run python -m unittest tests.test_spec_quality tests.test_spec_lock -v
git diff --check
```

Полный suite требует настоящих TEST_DATABASE_URL и TEST_KEYCLOAK_URL. Повторять
пройденные gates после дальнейших изменений только в затронутой области.

## Подтверждение этапа

Реализованы root admission и schema 13. В review найдены и устранены shutdown
между commit и SDK start, отсутствие memory terminal projection после recovery
и попытка legacy retention удалить enterprise history. Memory и PostgreSQL
используют общий mapper terminal Task; terminal projection не затирается
поздним SDK snapshot. Retention отклоняется до mutations.

Проверено: 64 targeted admission/auth tests; полный suite — 483 tests без skips
на настоящих PostgreSQL и Keycloak; Ruff, spec quality/hash lock, uv build и
Docker build прошли. В image подтверждены non-root startup, импорт admission
и schema 13. Spec review и последующий code/security review завершились без
оставшихся findings. Binary enterprise intake остаётся явно неподключённым
до atomic workspace/guardrail flow следующего этапа.
