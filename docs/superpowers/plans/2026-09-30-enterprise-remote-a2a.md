# Enterprise remote A2A — implementation plan

> Execute with subagent-driven-development or executing-plans. Read the normative
> spec first; preserve the shared worktree and assign explicit file ownership.

**Goal:** Владелец управляет доверенными агентами; отправка возвращает durable
handle, ожидание переживает restart и окончательно заканчивается по deadline
без повторной отправки задания или передачи входящих credentials наружу.

**Architecture:** Расширить существующий background task lifecycle remote kind,
текущий wait coordinator и owner settings. Реестр хранит immutable peer revisions
и зашифрованные header values. Отдельные remote workflow, model ledger и generic
polling framework не создаются. Сначала polling; SSE/push ускорение необязательно.

**Stack:** Существующие Python/A2A SDK, PostgreSQL/psycopg, Fernet, unittest и uv.

## Источники и исходное состояние

Нормативные источники: `spec/tasks-and-delegation.md` A2A-01, LONG-01/02,
`spec/tools.md`, `spec/agent-configuration.md`, `spec/public-contract.md` и
enterprise acceptance. Разрешение на frozen spec/tests от 29 сентября записано
в основном плане. Ниже уточняется техника согласованного поведения; это не
разрешение менять уже принятые продуктовые границы.

Исходный `remote_agents.py` — synchronous adapter с legacy ENV registry. Runtime
может передавать incoming credentials и root IDs; восстановление stream может
повторить Send, foreign human-wait трактуется как terminal. Эти пути заменяются
в одном связном срезе, а не объявляются durable после переименования инструмента.
Local task/time waits, owner HITL и guardrails уже имеют durable stores/coordinator.
Runtime chat continuity подключён; owner history API и UI остаются отдельной работой.

## 1. Уточнить контракт перед кодом

**Files:** профильные `spec/*.md`, `tests/test_spec_lock.py`, этот план.

- [x] Закрепить schema send `{agent_name, task}` и обычный локальный task snapshot
  в результате. Имена остаются `core_agent_send_message`, `core_task_get/list/wait/cancel`.
  Runtime-only `task_id` handle не является remote ID. Схема файлов добавляется
  вместе с рабочим file transport, без обещания поддержки до её реализации.
- [x] Deadline отсчитывать от первого разрешённого dispatch Send и сохранять до
  сети; owner HITL до этого не расходует remote timeout. Default 86400 секунд,
  poll interval300, оба company settings. Restart, follow-up и wait не меняют срок.
  Для remote handle аргумент `core_task_wait.timeout` отклонять с понятным
  structured результатом; local bounded wait сохраняет прежнюю семантику.
- [x] Explicit task cancel и existing owned-task cancellation сохраняют своё
  намерение до сети. При известном remote ID и непросроченной операции допустим
  один CancelTask; неопределённый outcome требует reconciliation. Timeout не
  вызывает CancelTask, последующий cancel не открывает сеть заново.
- [x] Зафиксировать owner-only registry routes, CAS, write-only secrets и
  совместимость settings PUT. Предлагаемый минимальный контракт:
  `GET/POST /api/remote-agents`, `PUT/DELETE /api/remote-agents/{id}`;
  DELETE отключает новые вызовы и сохраняет pinned revisions текущих операций.
  Точные request/response fields и validation внести в public contract прежде API.
- [ ] Binding/URL проверяется до использования credentials: userinfo и опасные
  redirects запрещены; чужой endpoint из Card требует явного trusted binding.
  Header name/value не меняют Host, framing или protocol headers и не содержат
  CR/LF/NUL. Проверить поддерживаемые A2A1.0 bindings по установленному SDK;
  два экземпляра нашего HTTP+JSON приложения должны уметь общаться между собой.
- [ ] Описать upgrade старого synchronous send/checkpoint: известный remote ID
  позволяет GetTask; неизвестный mutating outcome — reconciliation, без Send.
  Legacy ENV переносится только явным import, не перезаписывает owner registry.
  Rollback image обязан читать новую operation/schema version.

## 2. Registry и schema migration

**Files:** `database.py`, `remote_registry.py`, `remote_agents.py`, `interactions.py`, `owner_api.py`,
`app.py`; registry/auth/store tests; `.env.example`, Compose, README, AGENTS.

- [x] Следующая migration после schema18 добавляет scoped peer revisions и
  encrypted credential references; remote timeout/poll interval расширяют
  существующие company settings. Секретный envelope связывает значение с
  tenant/peer/revision/header name; использовать существующий Fernet/key pattern.
  Plaintext values не выдаются GET, audit, model, process или errors.
- [ ] Owner CAS update сохраняет предыдущую revision для принятых операций.
  Pending HITL связывается с проверенной destination revision: изменение registry
  не должно незаметно менять адресата уже согласуемого вызова.
- [ ] Read-only discovery имеет bounded retry. Только проверенные доступные
  разрешённые peers видны в описании инструмента. При пустом effective registry
  send отсутствует в catalog/Card и повторно запрещён при dispatch.
- [ ] Протестировать company scope, external/dual-role403, conflicting revisions,
  secret redaction, rotation и отключение peer без потери старого operation binding.
  Старые owner settings requests сохраняют новые settings, а не сбрасывают defaults.

## 3. Durable remote task и transport

**Files:** `tasks.py`, `postgres_tasks.py`, `remote_agents.py`, `runtime.py`,
`workflow.py`, `app.py`; соответствующие существующие unittest modules.

### Конкретный порядок интеграции по текущему scheduler

1. Transport предоставляет bounded Send/Get/Cancel для JSONRPC и HTTP+JSON1.0.
   Send использует `returnImmediately: true` и отдельный stable message ID,
   не принимает root task/context IDs. Card interface совпадает с зарегистрированным
   trusted endpoint; credential передаётся только выбранному peer. Get/Cancel
   кодируют remote ID как один URL segment. Headers не сохраняются в connection.
2. Добавить versioned checkpoint в существующий `core_background_tasks` следующей
   additive migration. `NULL` сохраняет прежний local task contract; неизвестная
   remote version отклоняется. Immutable contract содержит pinned peer revision,
   endpoint/binding и stable message ID; mutable checkpoint — dispatch marker,
   remote IDs, абсолютный deadline, pinned poll interval, next poll и cancel intent.
   Секрет остаётся в registry revision. Отдельный `core_runs` remote workflow
   и второй model ledger не создаются.
3. Существующий handler получает claim token. Один network step завершается
   pending marker с сохранением working и освобождением worker/claim; это не
   фиктивный `SuspendedRun` с отсутствующим run. Due filtering и проверка срока
   входят в existing claim/recovery, повторная проверка выполняется под row lock.
   Checkpoint и terminal state/result/error/notification/outbox фиксируются одной
   fenced scheduler transaction. Переиспользовать transaction body `_finish`.
4. Runtime закрепляет peer revision до HITL и включает binding в approval digest.
   После policy/guardrails создаёт deterministic local handle и admission marker
   атомарно с parent snapshot через existing `start(admission=...)`. Эта ветка
   восстанавливается как task admission, не как неизвестный generic EXECUTING.
5. Coordinator сначала окончательно закрывает просроченную remote operation,
   затем применяет existing task wait. `core_waits` не хранит второй independent
   remote deadline; authoritative deadline остаётся в operation checkpoint.
   Generic cancel не должен заменить reconciliation неизвестного Send/Cancel
   обычным canceled. После timeout никаких новых outbound requests.

Каждая часть имеет общий memory/PG regression contract и runtime wire proof.
Storage/public timeout result shape уточнить в normative spec до implementation;
наличие transport методов не закрывает LONG acceptance без integrated recovery.

- [ ] Remote kind использует existing task handle/ownership/mailbox. Versioned
  state содержит pinned peer revision, stable message ID, dispatch marker,
  remote task/context IDs, absolute deadline, next poll и окончательный outcome.
  Local task rows не меняют семантику. Новое schema state регистрируется обычной
  migration, serving process не изменяет DB автоматически.
- [ ] Зафиксировать operation и mutation intent до Send. Root task/context IDs
  не передаются как remote IDs, incoming Authorization/API keys не проксируются.
  ReturnImmediately и выбранный binding соответствуют SDK contract. Immediate
  terminal Message тоже завершает handle; отсутствие remote task ID не повод Send повторять.
- [ ] При известном remote ID recovery делает GetTask, а не повторяет Send.
  При crash после dispatch до ID commit неизвестный outcome сохраняется как
  reconciliation. Stable message ID не считается гарантией downstream dedup.
- [ ] Poller использует existing recovery coordinator, короткую claim и bounded
  network request вне transaction. Между проверками compute lease освобождена;
  model/tool budget не расходуется. Несколько pollers не публикуют result дважды.
- [ ] Foreign input-required/auth-required остаются working. В progress отдавать
  проверенные status metadata; произвольный remote text/result проходит existing
  guardrails до model/UI/public projection. Foreign HITL решает remote owner.
- [ ] GetTask/list — scoped persisted snapshots, wait — existing durable task wait.
  На terminal notification continuation применяется ровно один раз. Повторный
  tool/result dispatch при восстановлении запрещён.

## 4. Deadline, cancellation и поздний ответ

- [ ] Перед каждым request проверить current claim, cancellation и deadline.
  Terminal result и timeout конкурируют в одной fenced transaction; победитель
  фиксирует task outcome/mailbox и снимает next poll.
- [ ] После timeout никаких GetTask, подписок, reconnect или CancelTask, включая
  restart и повторный wait. Возвращать сохранённое объяснение: срок истёк,
  исход предыдущей операции неизвестен, новую Task можно создать отдельным send.
- [ ] Late in-flight response не меняет outcome и не создаёт model/UI message,
  guardrail request или второе continuation. Таймаут не доказывает отсутствие
  удалённого side effect. Unknown mutation не превращается в безопасный retry.
- [ ] Explicit cancel без remote ID учитывает pre-dispatch/unknown-dispatch
  различие; после отправки CancelTask его неизвестный outcome не повторяется
  автоматически. Parent budget/terminal barrier сохраняют существующие гарантии
  owned-task cleanup и reconciliation.

## 5. Файлы и проверки

- [ ] Outgoing files подключить к immutable final-file preparation из
  `2026-09-30-enterprise-files.md`: relative paths, whole-batch limit и manifest
  до первого Send. Remote inbound batch проходит quarantine/guardrails целиком.
  Неподдерживаемые Parts дают явную ошибку до публикации, а не молчаливую потерю.
  Text-only этап не закрывает FILE acceptance и весь remote срез.
- [ ] Wire tests: оба объявленных поддерживаемых bindings, отсутствие root-ID/
  credential relay, explicit configured header only, redirect rejection,
  immediate Message, foreign HITL, malformed/foreign remote IDs, files limit.
- [ ] Durable tests: crash до/после Send/ID commit, concurrent pollers,
  timeout-versus-result, rewait/restart, late response, cancellation unknown outcome,
  owner/child scope, registry rotation. Memory и PostgreSQL реализуют один контракт.
- [ ] Выполнить applicable modules `tests.test_transfer_features`,
  `tests.test_durable_waits`, `tests.test_wait_store`, `tests.test_task_suspension`,
  `tests.test_interaction_store`, `tests.test_postgres_persistence`, новые remote
  tests; затем Ruff и full suite через uv. Mock opener/direct ASGI проверяют wire
  без socket bind, но не заменяют live network/PG/Keycloak/native release gates.
- [ ] Обновить tools/kernel/config/examples/AGENTS по фактическому flow. Отметка
  implemented допустима только с обычным CI proof, не по наличию этого плана.

## Проверенный срез: хранение и owner API реестра

Schema19, memory/PostgreSQL stores и authenticated composition root подключены.
Owner CRUD хранит immutable revisions и write-only Fernet credentials; metadata
pagination использует общий bounded cursor parser с `/api/chats`. Настройки
remote timeout/poll interval расширяют settings PUT совместимо со старым клиентом.
Проверки до SQL закрывают NUL в descriptions и decoded peer IDs, сохраняя 404
для корректных неизвестных ID. Spec/security и code-quality reviews завершены.

Focused gate `uv run python -m unittest tests.test_remote_registry_api
tests.test_owner_chats tests.test_remote_registry tests.test_interaction_store
tests.test_interactions tests.test_auth tests.test_spec_lock tests.test_spec_quality
-q` проходит: 138 tests, 65 PostgreSQL skips. `uv run ruff check core_agent tests`
и `git diff --check` проходят. Полный suite: 961 tests, 6 failures, 34 errors,
242 skips; список падений совпадает с предыдущим прогоном. Socket bind запрещён
в текущем окружении, live PostgreSQL/Keycloak/native Linux недоступны. Пропуски
не подтверждают migrations, DB role grants и PostgreSQL races.

На этапе registry storage подключение entries к runtime ещё отсутствовало.
Текущий integrated text-only срез описан ниже; файлы и live release gates
не закрываются проверками реестра.

## Проверенный срез: A2A transport

Existing `remote_agents.py` расширен отдельными nonblocking send_task/get_task/
cancel_task без изменения legacy handlers. Поддержаны JSONRPC и HTTP+JSON1.0,
`returnImmediately`, pinned Card discovery, private peer headers, проверенные
remote IDs и сохранение всех Parts. Immediate Message завершён, foreign
input-required/auth-required нетерминальны. Authenticated direct ASGI тест
настоящего SDK приложения проверяет Card → Send → Get → Cancel при занятой модели
без TCP listener. JSONRPC wire проверен по установленному SDK.

Spec/security и quality reviews завершены. Exact focused gate включает
`tests.test_remote_transport`, `tests.test_transfer_features.ForwardedHeaderTests`,
`tests.test_transfer_features.RemoteAgentConnectionTests.test_peer_without_a_jsonrpc_1_0_interface_is_refused`
и `tests.test_tool_approvals.ToolApprovalTests.test_remote_agent_intermediate_parts_stay_private_for_external_task`:
21 tests проходят, skips нет. Проверки воспроизводили и закрывают mismatch URL
semicolon parameters, protobuf OverflowError для nonfinite state, malformed
optional Message IDs и отсутствие bounded discovery5xx retry. Mutations не
повторяются. Этот срез не закрывает durable deadline/cancel-once/recovery или
файловый transport и не подключает send handler к owner registry.

После integration проверены `tests.test_remote_transport`, registry API/store,
tool approvals, guardrails, owner chats/interactions, auth и spec lock/quality:
172 tests проходят, 53 PostgreSQL skips. Ruff для всего `core_agent tests` и
diff check проходят. Full suite теперь979 tests: прежние6 failures/34 errors,
242 skips; новых падений нет, список точно совпадает с history baseline.
Это локальные доказательства adapter/API и отсутствие новых обнаруженных
регрессий, не проходящий release gate для отсутствующих live сервисов/sandbox.

## Проверенный срез: integrated text-only operations и progress

Schema20 добавляет nullable checkpoint существующим background tasks. Remote
handler работает через fenced scheduler claims, immutable peer revision и
Send/Cancel intent до сети; recovery не повторяет mutating request. Deadline
устанавливается server clock первого разрешённого Send. Timeout окончательный,
поздние ответы игнорируются, temporary Get failures планируют следующую проверку.
Authenticated runtime использует только company registry; peer/settings/message
ID закрепляются до HITL, admission marker и parent snapshot коммитятся атомарно.
Nested Python и child delegation используют тот же flow. Legacy direct runtime
оставлен отдельно; автоматического ENV import нет.

TaskStore/Get/List/Subscribe/push показывают safe working metadata, не публикуя
private peer text/IDs/HITL. Одинаковый status не создаёт повторных events;
expired operation удаляется до coordinator tick. Fixed regressions: stale live
Task frame стирал metadata; projection вызывала model-visible list во время HITL.
Internal read-only scheduler API и scoped live-frame overlay закрывают причины.

Final focused gate: `uv run --offline --no-sync python -m unittest
tests.test_remote_progress tests.test_wait_store tests.test_remote_runtime
tests.test_remote_operations tests.test_remote_scheduler tests.test_postgres_persistence
tests.test_interactions.OwnerRuntimeAPITests.test_rejection_returns_tool_error_without_execution
-q` —149 tests,79 executed,70 PostgreSQL skips,exit0. Independent spec/security
и quality reviews прошли тот же gate. Full suite после UI hook:1071 tests,
6 failures,34 errors,267 skips; исправленная HITL regression исчезла, новых падений
нет. Whole-repo Ruff и diff check проходят. Live PostgreSQL, Keycloak, socket
network и native sandbox gates остаются неподтверждёнными. File/data Parts
executor пока отклоняет явно; этот срез не закрывает FILE или полный release.

## Порядок поставки

Сначала normative contracts и migration; затем registry+adapter и durable lifecycle
одним совместимым flow; далее files, owner UI и release gates. Первым reviewable
implementation change может стать registry storage/API, но synchronous legacy
send не объявляется готовым и не должен выдавать новое неблокирующее поведение
до подключения operation state/recovery. Требования исходного enterprise плана
сохраняются полностью.

## Файловый срез: уточнение по подключённому коду 1 октября 2026

Исходящий срез сохранён в `a3098f2`; terminal inbound import остаётся следующим
этапом. Частичные gates не означают готовность полного remote file flow.
Нормативные изменения в `spec/` и frozen lock входят в тот же разрешённый срез.

- [x] В `spec/tasks-and-delegation.md`, `spec/artifacts.md`, public contract и
  acceptance закрепить optional `files` с относительными paths, pinned batch
  ceiling, versioned job compatibility и импорт полного terminal результата.
  Модель явно выбирает список для каждого `core_agent_send_message`; отсутствие
  `files` или `[]` означает отправку без файлов. Содержимое workspace, недавно
  созданные файлы и выбор `core_response_files` автоматически не добавляются.
  `input-required`/`auth-required` сохраняют working и те же remote IDs;
  промежуточные файлы не публикуются. Failed/cancelled/expired операции не
  публикуют refs. Progressive import не добавляется без отдельного контракта.
- [x] В `runtime.py:_pin_remote_call` подготовить полный outbound snapshot до
  HITL, используя `ResponseFileService.prepare`. Не менять final selection
  `core_response_files`. Закрепить ordered public receipts и selection digest
  в approval subject; private refs остаются в persisted remote contract.
- [x] В `tasks.py`, `postgres_tasks.py`, `remote_operations.py` добавить job v2:
  существующие peer/message/settings, trusted `caller_scope` с owner/context/
  source task/run, `attachment_limit_bytes`, frozen `outgoing_files`.
  Scheduler `owner_id` остаётся owning run ID. v1 читается по прежнему text-only
  протоколу, без реконструкции refs. До `send_started` загрузить и проверить
  весь outbound набор; неизвестный исход Send не повторять.
- [x] В `remote_agents.py` общий `_task_request` обеих bindings передаёт text
  и standard raw Parts с pinned Message ID. Finite encoded ceiling должен
  вмещать 25 000 000 decoded bytes с base64/JSON (нынешние 16 MiB недостаточны).
  Перед protobuf проверять полный bounded body, UTF-8/JSON, duplicate keys,
  depth и canonical base64; до relay проверять decoded aggregate. Terminal
  Task собирает весь ordered Artifact file set даже при status Message с text.
- [x] В `remote_operations.py`, scheduler stores и `chat_files.py` атомарно
  фиксировать fenced terminal result и accepted quarantine batch, используя
  одну PostgreSQL connection. Late cancel/deadline/lost claim не принимают
  batch; собственный проигравший stage очищается после выхода из transaction.
  Remote IDs являются provenance, не authority для локального binding.
- [x] Сохранить FK на canonical public A2A Task и core_run. Root send использует
  существующий bind. Delegated child не имеет публичной A2A admission row:
  проверить canonical root/child ancestry для batch binding, сохранив child
  continuation/lease и root liveness. Не создавать shadow Tasks и не ослаблять FK.
- [ ] В `runtime.py:_guard_file_batch` поддержать существующие tool-result/
  Python nested continuation и authorized batch binding. До model/catalog/
  history receipt проверить весь remote text/file batch и пройти существующий
  publication barrier. `_task_snapshot` исключает private batch refs. Owner
  sealed review/download проверяет ту же root/child ancestry. Отказ/timeout
  исключает материал и продолжает задачу без него; quarantine переживает restart.
- [ ] Регрессии: zero Send при ошибке всего набора, frozen bytes до/после HITL,
  обе bindings и empty file, malformed/oversized последний Part без partial
  relay, v1 recovery, foreign human wait и same-ID final, pool1 atomic bind,
  child isolation, guard allow/reject/timeout/restart, identical-byte foreign
  scope, cancel/deadline race и отсутствие post-failure refs. Использовать
  existing HTTP server fixtures и реальный PostgreSQL для network proof.

Порядок владения: root — `runtime.py`, `app.py`, owner review integration и
norm/lock; transport — `remote_agents.py`/bounded decoder; scheduler —
`tasks.py`, `postgres_tasks.py`, `remote_operations.py`; files — `chat_files.py`
и shared manifest helpers. Два разработчика не меняют один файл одновременно.
Проверки каждого среза используют существующий `unittest` engine; итоговый
`uv run python -m unittest discover -s tests -v` запускается с PostgreSQL и
Keycloak. Файловая возможность не объявляется поддержанной до прохождения
полного admission/guard/publication flow.

Для inbound bind сохраняется существующая схема: `batch.run_id` — source child
или root run, `batch.task_id` — derived public root Task. Shared
`ChatFileService.caller_scope(binding, task_id, run_id, connection=None,
lease_token=None, accept_input=False)` возвращает source/root/connection под
locks chat → root → intermediate → source. Mutations проверяют всю canonical
ancestry и liveness; optional token fence относится только к source, а historical
sealed read допускает завершённые workflows. Root IDs из request/job metadata
не создают authority. Два parent links — maximum; другой чат/owner/tenant,
cycle, отменённый ancestor и stale source lease закрывают mutation.

Remote `commit_remote_claim` получает optional local `prepared_file_batch`
ровно `{batch_id, lease_token, actor_id, message_id, request_digest}`. Source
берётся из persisted job v2; service не сериализуется в contract. PostgreSQL
держит одну connection и после canonical locks берёт remote job, затем batch;
после slow bind повторно проверяет fresh server time/claim/revision/cancel и
source. Terminal result, accepted quarantine и notification/outbox commit-ятся
вместе. Memory соблюдает workflow → scheduler → batch и откатывает эти изменения
при ошибке. Проигравший stage очищается после rollback, а не внутри transaction.
Private result содержит только `file_batch_id`; marker исключается из
mailbox/outbox и `_task_snapshot`. Runtime refetch-ит scoped authoritative
scheduler Task и проверяет text/files до публикации и передачи модели.

После outbound redirect/early-preview исправлений полный fresh PostgreSQL /
Keycloak suite прошёл: 1501 tests, 196.434 секунды, exit0, три ожидаемых skips;
Ruff/diff-check прошли. Детальные evidence и ограничения actual child proof
сохранены в основном enterprise/file plan. Эти проверки не закрывают inbound
atomic bind/guardrail пункты выше.

Atomic quarantine/source foundation проверен отдельно до composition wiring:
native scheduler/executor gate —96 tests, 5.064 секунды, exit0, без skips;
canonical files/owner review gate —68 tests, 4.508 секунды, exit0, без skips.
Реальные PostgreSQL workflows child/grandchild используют только одну public
root Task row и pool1. Проверены cancel/ancestor/claim/deadline races после slow
bind, notification rollback, late duplicate stage cleanup, restart без Send,
marker-free mailbox/outbox и owner-only sealed read из root chat, включая
historical terminal sources. Independent review выявил Latin-1 reflected header
bytes: новый memory/PG regression подтверждает отказ до stage для actual wire
и UTF-8 representations без изменения bytes/digest. Ruff и diff-check прошли.
Evidence — `.local-evidence/remote-inbound/final-targeted.*`, `red-latin1.*` и
`.local-evidence/chat-files-children/owner-final.*`. Этот foundation не включает
runtime text/file guard, publication receipt и app wiring следующего среза;
полный FILE-03 остаётся open.
