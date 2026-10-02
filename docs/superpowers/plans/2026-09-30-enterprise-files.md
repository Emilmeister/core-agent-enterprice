# Enterprise chat workspace and files — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax. This plan does not authorize unrelated changes or replace the repository's frozen spec/tests rules.

**Goal:** Постоянная папка чата, атомарный приём полного набора вложений и выдача проверенных файлов через owner UI/A2A с сохранением caller scope, dedup и recovery.

**Architecture:** Один доверенный resolver связывает run с immutable chat owner и постоянным workspace. Входящие файлы сначала полностью сохраняются вне sandbox, затем durable admission и решение guardrails разрешают публикацию одного подготовленного каталога; runtime получает сообщение только после публикации. Исходящий набор заранее превращается в immutable transport blobs, а права выдачи следуют из сохранённой привязки к чату и Task.

**Tech Stack:** Существующие Python, pathlib/os/hashlib, PostgreSQL/psycopg, Starlette/A2A SDK, unittest и uv. Новые storage SDK, очередь и test framework не требуются. Bubblewrap и Python broker mount реализуются по отдельному [sandbox plan](2026-09-30-enterprise-sandbox.md).

## Статус, зависимости и порядок поставки

Это план, а не доказательство реализации. База — auth `2adce49` и следующий admission-срез со schema 13: `core_chats` и `core_root_messages`. Перед исполнением сверить фактическую последнюю миграцию: номер 14 ниже означает следующую после 13, а не разрешение переиспользовать занятый номер.

Нормативные источники: `spec/artifacts.md` FILE-02–04/CLEAN-01, `spec/execution-environment.md`, `spec/public-contract.md`, `spec/security-and-reliability.md`, `spec/acceptance.md` ENT-AC-11, 17a, 58–66, 68. RunRequest по-прежнему содержит ровно `prompt`; bytes, transport file metadata и auth не становятся его публичными полями.

Последовательность: trusted workspace binding → durable private storage → реальный durable interaction/guardrail path и sandbox → включение binary admission → final files/UI delivery → удаление старого artifact service. До реального guardrail decision path enterprise FileParts продолжают получать `CONTENT_TYPE_NOT_SUPPORTED` до admission и файловых эффектов. Существование publisher или тестового classifier не разрешает выставить production `allow` по умолчанию. До Linux sandbox proof нельзя объявлять process isolation реализованной.

Owner UI и remote durable waits — отдельные срезы. Этот план задаёт их файловые API/интеграцию; backend-тест не доказывает browser flow или durable remote execution. Во время реализации закрывать только те строки implementation-status, для которых обычный CI выполняет соответствующий end-to-end proof.

## Наблюдения по текущему коду

| Место | Наблюдаемое поведение и необходимая общая точка изменения |
|---|---|
| `core_agent/app.py:store_attachments` | Per-file `artifact_service.save`, затем добавление references в prompt; вызывается из обычного handle/followup до старого runtime admission. Удалять только после замены обоих путей. |
| `core_agent/admission.py:PostgresRootAdmission._admit` | Dedup lock → chat row lock → Task/workflow/initial lease/request ledger одной транзакцией. Сюда входит связь initial input batch; busy попытка не создаёт run. |
| `core_agent/config.py:RunRequest`, `core_agent/a2a.py:parse_run_request` | Transport attachments не входят в `to_dict`; file-only prompt уже поддержан parser-ом. Сохранение только RunRequest теряет файлы после restart. |
| `core_agent/execution.py:LocalTerminalBackend.create`, `_LocalTerminalSession.destroy` | Папка run/workspace копирует snapshot, затем удаляется вместе с parent directory. Это непригодно для persistent chat tree. |
| `core_agent/execution.py:TerminalSessionManager.execute_transient` | Создаёт EnvironmentSpec с tenant=`default`, owner=run_id; реальный tenant/context теряется. |
| `core_agent/tools.py:ToolRuntime._execute`, `core_agent/python_exec.py:execute_python` | Оба вызывают execute_transient. Переданные в ToolRuntime identity/session/tenant сами не формируют EnvironmentSpec. |
| `core_agent/runtime.py:_task_start`, `_recover_background_tool`, `_delegate`, `_child_agent` | Background использует отдельный synthetic run ID; child и recovery должны явно получить тот же chat binding, сохраняя отдельные process/session owners. |
| `core_agent/runtime.py:RunResult`, `_record_tool_outcome` и terminal result branches | RunResult содержит только текст и completion provenance. Новый файловый результат нужно сохранять вместе с workflow outcome и восстанавливать во всех ветках. |
| `core_agent/app.py:result_artifact`, `core_agent/a2a_sdk.py:CoreAgentExecutor._publish_artifact` | Результат сейчас текстовый; publisher превращает все parts в строки. Добавление FilePart только в одном месте приведёт к потере bytes или строковому представлению файла. |
| `core_agent/remote_agents.py:RemoteAgentConnection._request`, `_normalize` | Исходящий remote message и результат ориентированы на текст. Файлы требуют общей подготовки и парсинга; credentials текущего HTTP caller не пересылаются. |
| `core_agent/artifacts.py` | Transport/offload storage сохраняется. In-memory ID дедуплицируется по tenant/content digest; PostgreSQL blob bytes — по digest, artifact ID учитывает provenance. Ни один blob ID не заменяет chat/task authorization. |
| `tests/test_local_terminal.py`, `test_python_exec.py`, `test_transfer_features.py`, `test_end_to_end.py`, `test_admission.py`, `test_postgres_persistence.py` | Существующие unittest fixtures, scripted model, HTTP stub, real PostgreSQL и `TemporaryDirectory` покрывают нужные уровни; новый test framework не нужен. |

Имена функций — навигация по прочитанному коду, а не обещание неизменных line numbers. Parent продолжает admission-срез; не перезаписывать его незавершённые изменения.

## Конкретные инженерные контракты

### Workspace и trusted scope

`CHAT_WORKSPACE_ROOT` — обязательный persistent POSIX volume для enterprise файлов. Его нельзя подменять `LOCAL_WORKSPACE_ROOT` или `DURABLE_STORAGE_ROOT` и нельзя молча включать S3 filesystem semantics. На старте проверить отсутствие пересечения с ephemeral root и наличие возможности fsync/atomic rename на томе. Ошибка конфигурации останавливает файловую capability до первого принятого файла.

Физическое размещение:

```text
CHAT_WORKSPACE_ROOT/
  chats/<tenant-key>/<owner-key>/<context-key>/workspace/
    attachments/<batch-id>/<actual-safe-name>
    ... writable files produced by the agent ...
  private/uploads/<upload-id>/        # technical staging, never mounted
  private/quarantine/<batch-id>/      # accepted, awaiting a real decision
```

Ключи — server-generated deterministic hash компонентов, не caller-supplied path segments; authoritative scope остаётся в БД. Tenant берётся из verified deployment context, owner — из `core_chats.owner_id`, context — из сохранённого workflow/chat. `actor_id` нужен для dedup/audit и не подменяет execution owner: owner, работающий в external чате, открывает прежнюю папку этого чата.

Добавить один внутренний `WorkspaceBinding(tenant_id, owner_id, context_id)` и метод `TerminalSessionManager.bind_run(run_id, binding)`. Revision добавляется только вместе с реальным cleanup contract, не заранее. Bind разрешён только composition/runtime из durable record; повтор с иным scope отклоняется. `execute_transient` требует binding и не угадывает `default`/anonymous. `EnvironmentSpec` хранит binding отдельно от process/session owner. Background synthetic ID получает binding при старте и из сохранённого task contract при recovery. Child получает родительский chat binding; optional scratch остаётся под `LOCAL_WORKSPACE_ROOT` и явно помечается ephemeral. Независимые cwd, PTY, process groups и budgets сохраняются.

`LocalTerminalBackend.create` открывает существующий chat workspace, а не materialize-ит старый snapshot поверх него. `destroy` завершает owned процессы и удаляет только ephemeral session/scratch state. Он не удаляет chat tree при success, failure, cancellation, lease loss или shutdown. Сохранять snapshot можно, восстанавливать существующий persistent workspace из него автоматически нельзя. Неизвестная legacy ownership блокирует enterprise binding до явной migration mapping.

Sandbox launcher получает от resolver конкретный host workspace, bind-ит только его в `/workspace`, затем mandatory `readonly_input_path` bind-ит host attachments поверх `/workspace/attachments` read-only; private staging/quarantine не находятся внутри этих binds. Input редактируется через копию в writable chat tree. Python broker получает только текущий socket в `/run/core-agent/broker.sock`; host path/token — internal launch arguments, не model schema, не env credentials. Sandbox plan отвечает за host/PID/network isolation и закрытие лишних descriptors. Path resolve сам по себе этим доказательством не является.

### Durable input manifest и schema migration

Следующая migration добавляет:

1. `core_chat_file_batches`: `batch_id` PK; `schema_version=1`; trusted tenant и actor/message; context/immutable owner и task/run/sequence nullable до admission; `request_digest`; `created_at`; upload lease owner/token/expiry; `state`; `manifest jsonb` (immutable после acceptance); `published_at`; guardrail decision reference. Nullable FK `(tenant_id, context_id)` связывается с созданным чатом в admission transaction; stage неизвестного нового context ещё не является chat ownership claim. До этого заявленный context хранится только как untrusted request metadata. Accepted state требует NOT NULL context/owner/task/run и complete manifest; sequence отсутствует только у initial root batch.
2. `core_inbound_messages.file_batch_id` nullable FK и `delivery_state` (`ready`, `pending_files`, `excluded_material`) с default `ready`. Existing text rows остаются готовыми. Initial root batch ID хранится в schema-versioned workflow snapshot; root-message ledger остаётся authority dedup.
3. `core_chats.workspace_revision` с начальным 0 и durable records удаления путей с revision; они нужны FILE-04 и запрету resurrection. Если cleanup реализуется отдельным commit, добавить эти колонки/таблицу его следующей миграцией, а не оставлять неиспользуемую таблицу заранее.

Минимальные состояния batch: `staging` (не принято), `accepted_quarantine`, `accepted_ready`, `published`, `excluded`, `rejected`. Это storage/publication state, не параллельная A2A Task state machine. Технически подготовленный batch остаётся `staging` до общей admission transaction. Переход в accepted требует полного immutable manifest и durable file bytes. `accepted_ready` означает irreversible acceptance плюс записанное разрешающее guardrail решение; runtime не может сам записать allow.

Manifest хранит `schema_version`, ordered entries `{index, original_name, actual_name, relative_path, media_type, size_bytes, sha256}`, aggregate decoded bytes, original file metadata и transport source. Байты не помещаются в RunRequest или active model context. Состояние `staging` также сохраняет original `created_at`, heartbeat/lease и server-owned relative storage key; filesystem mtime и время restart не являются возрастом upload.

Fingerprint version 1 admission сохраняется: hash исходного canonical Message до назначения Task/context IDs и до sanitization имён. Inline file bytes и исходные metadata уже входят в него. Не пересчитывать digest из преобразованного prompt или фактических suffix names. Старые ledger rows проверяются по сохранённой версии; неизвестная версия даёт safe refusal. Нет необходимости менять format текстового dedup ради новой storage таблицы.

Не держать транзакцию БД во время HTTP upload. Структурные/schema проверки, body limits и все writes/fsync выполняются до admission. Row `staging` связывается с accepted input в той же транзакции, где создаются root Task/workflow/ledger, либо где вставляется follow-up inbox row. Raw file payload в SDK Task history не должен превратиться в tool/model context: authoritative manifest хранится отдельно, а отображение history и attachment references сохраняет scope. Если SDK записывает raw Parts повторно, использовать существующую dedup history integration и проверять, что bytes никогда не интерпретируются как prompt.

### Приём и публикация полного сообщения

Один service в новом `core_agent/chat_files.py` обслуживает A2A, будущий owner UI и remote file ingress. Без plugin registry, backend factory и отдельного S3 adapter. Публичные входы различаются transport decoding, общие операции — prepare, bind to admission transaction, publish, list/open scoped files и cleanup.

1. Авторизовать caller и проверить message schema/ID/declared context. Ссылочные Parts без trusted resolver отклоняются целиком с `CONTENT_TYPE_NOT_SUPPORTED`; не выполнять caller URL fetch. Base64 decoding строгое, decoded bytes суммируются по всему сообщению. Default limit ровно 25_000_000; одна platform setting обслуживает вход/выход/UI/A2A. Пока UI settings ещё нет, setting хранится централизованно с этим default и не дублируется в adapter constants. Позднее UI меняет тот же источник. Proxy/body limit отдельно допускает base64 и JSON overhead.
2. Сохранить **все** файлы под временными индексами в server-owned stage на том же volume, проверить фактические sizes/digests, закрыть и fsync файлы, provisional manifest и directory. При любом decode/write/fsync/limit error удалить новые staged bytes, записать rejection, не создавать run, не enqueue-ить follow-up и не менять активную Task. Вернуть безопасную причину; размерная ошибка содержит `allowed_bytes` и `actual_bytes`. Файлы нулевого размера допустимы и требуют проверки существующего ArtifactStore, который сейчас может отвергать пустой content.
3. В admission transaction соблюдать порядок locks: stable root dedup key → chat row → batch row; для follow-up chat row → workflow/inbox → batch row. Не вводить обратный порядок в publisher/cleanup. Existing duplicate возвращает прежнюю Task и её file receipts; новый stage дубликата удаляется. Конфликт digest отклоняет новое сообщение и stage. `CONTEXT_BUSY` создаёт предусмотренную failed Task без workflow и без публикации файлов попытки.
4. Выбрать actual safe names в chat namespace под тем же lock: имя без path/drive/NUL/control traversal, пустое/непригодное → `attachment-N` с безопасным расширением. Сохранять исходное имя как недоверенную metadata. Занятые имена файлов, каталогов и symlinks, имена уже зарезервированных accepted batches и дубликаты внутри сообщения получают `_2`, `_3` перед расширением. Внутри private stage переименовать индексные файлы, записать окончательный manifest и fsync directory до admission commit; ошибка откатывает admission и удаляет stage. Новый batch directory уже содержит полный набор. Names и пути видны одинаково в UI receipt, A2A metadata и дальнейшем user turn.
5. Commit Task/inbox/dedup + complete manifest + storage intent. Пока guardrails не разрешил материал, accepted batch остаётся только в private quarantine. Техническое принятие полного сообщения и разрешение раскрыть его модели — разные durable факты. Detector failure открывает реальный owner decision; нет автоматического allow или bypass через timeout. Owner reject создаёт безопасное уведомление без содержимого; accepted материал не выдаётся модели, дальнейшее выполнение следует GUARD-01–04.
6. Только `accepted_ready` batch публикуется одним **no-replace atomic rename каталога** в `attachments/<batch-id>`, затем fsync родителя. Нельзя заменять этот шаг циклом individual renames/hardlinks. Не использовать check-then-rename, который способен заменить существующий directory: финальная операция должна иметь no-replace semantics либо работать в защищённом от sandbox записи namespace. Все source/target components открываются через trusted directory descriptors, без symlink traversal. Batch ID — identity, а не мера защиты пути.
7. После rename отдельная transaction проверяет manifest/location, помечает `published`, делает inbox ready и будит runtime через существующий durable outbox. Root initialization/MCP/model и follow-up draining проверяют publication barrier; до него prompt и file references не доставляются. Inbox row с `pending_files` считается непрочитанным для terminalization. Следующие сообщения не обходят его в установленном порядке. При cancel/failure такой input записывается как unprocessed вместе с terminal transition; recovery не публикует его задним числом в закрытую Task.

**Почему commit precedes rename:** живой terminal/Python процесс видит rename немедленно. PostgreSQL lock или запрет новых commands не скрывает каталог от уже запущенного процесса. Поэтому при публикации accepted intent и allow уже должны быть durable; откат транзакции больше не может превратить увиденный набор в rejected upload. All-or-nothing storage failure проверяется при подготовке durable stage. После durable acceptance временная ошибка публикации — pending accepted delivery с recovery, а не HTTP-отказ «сообщение не принято», после которого файлы неожиданно появятся.

Если после acceptance обнаружена необратимая порча/missing bytes, не доставлять текст отдельно и не терять input молча: safe failure/reconciliation, durable error и unprocessed transcript, исходные данные сохраняются. Операционный retry допускается только для публикации уже принятого immutable набора; неизвестный внешний side effect этим механизмом не повторяется.

Для namespace `attachments` sandbox plan закрепляет отдельный обязательный read-only bind внутри `/workspace`: сервер публикует на host side, агент копирует входной файл в writable tree для изменения. Работающий процесс не может rename/unmount/подменить mountpoint. Server удерживает trusted directory descriptor именно смонтированного attachments inode и не переоткрывает его через потенциальный symlink. Использовать no-follow/no-replace операции; конфликт target оставляет batch pending/reconciliation и никогда не даёт overwrite/escape. Не открывать binary intake, пока профиль не проходит hostile process race test.

### Crash recovery и sweeper

| Момент сбоя | Обязательное действие recovery |
|---|---|
| До complete stage/до admission commit | Ни Task, ни model input, ни public files; rejected bytes удалить сразу, orphan — до 24 часов с original created_at. |
| После durable acceptance, до guardrail decision | Сохранить quarantine и прежнее ожидание; не публиковать и не очищать как orphan. |
| После allow/accepted_ready, до rename | Проверить manifest/digests и завершить одну публикацию того же batch; IDs и имена не выбирать заново. |
| После rename, до DB published | Найти существующий target, сверить manifest/digests и завершить DB barrier; не делать вторую копию или user turn. |
| После published, до model consumption | Existing inbox/checkpoint sequence доставляет сообщение один раз; duplicate HTTP не создаёт новую копию. |
| Cancel/terminal конкурирует с allow/publication | Под общим chat/workflow serialization либо публикация разрешена до terminal decision, либо input помечен unprocessed/excluded и больше не публикуется. Нельзя publish по stale allow row. |

Publisher держит chat lock при проверке текущего Task/decision и rename/final barrier; accepted decision был committed **до** входа в этот участок. Внутри него terminal/cancel path должен участвовать в той же serialization. Это закрывает race публикации после committed cancellation, а не скрывает ещё не принятое содержимое от процессов.

Sweeper работает в существующем lifecycle/recovery loop, с bounded batch и startup pass. Для удаления unaccepted stage сначала под row lock повторно проверить lease/expiry и отсутствие accepted task/inbox/guardrail reference. Активная upload lease продлевается во время записи. Просроченные unreferenced uploads удаляются при startup; hourly sweep выбирает уже осиротевшие остатки с возрастом от 23 часов, оставляя запас до предела 24 часа. При backlog next pass выполняется немедленно, а не через следующий час. Orphan directory без row содержит durable original-age manifest; отсутствие/порча metadata не разрешает удалить accepted directory: сначала сверить все authoritative references. Restart не меняет age. Sweeper не удаляет chat workspace, accepted/quarantined files или старые пользовательские blobs.

### Минимальный model-facing output interface

Добавить один built-in `core_response_files`:

```json
{
  "type": "object",
  "properties": {
    "paths": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": true}
  },
  "required": ["paths"],
  "additionalProperties": false
}
```

`paths` — относительные пути существующих regular files своего workspace, без URLs, bytes, absolute paths и traversal. Вызов заменяет **весь** pending attachment set следующего final response; `[]` очищает его. Tool не отправляет сообщения наружу и не сохраняет именованные версии файлов. Он проходит обычные EffectiveConfig, tool budget, policy/HITL, scope и audit, включая вызов через Python broker и child allowlist.

Весь новый набор сначала безопасно открывается и копируется в immutable transport storage. Проверяются actual aggregate byte count, type, file integrity и отсутствие path escape; source file handles держатся при чтении, повторные size/identity checks обнаруживают замену. Ни существующие файлы, ни прежний pending set не меняются при ошибке. Oversize возвращает structured `ATTACHMENTS_TOO_LARGE` с `allowed_bytes`, `actual_bytes`; остальные причины — `FILE_NOT_FOUND`, `INVALID_FILE_PATH`, `FILE_CHANGED`, `FILE_READ_FAILED`, `ARTIFACT_INTEGRITY_FAILED`. Это recoverable tool result, не автоматический failed Task. Частично подготовленные unreferenced output blobs убираются по безопасной storage cleanup policy; workspace originals остаются.

Успех возвращает ordered receipts `{file_id, name, media_type, size_bytes, sha256}`. В `_record_tool_outcome` новая schema-versioned pending manifest и tool receipt фиксируются одной workflow transaction; сбой до commit сохраняет старый set. Recovery с уже committed receipt не перечитывает изменившийся workspace. В child результат относится к child и не публикуется напрямую caller-у root; parent получает scoped references как недоверенный child result и явно выбирает свой финальный набор.

`RunResult` расширяется `outgoing_files=()`; `to_dict` и все live/recovery/partial-final result branches сохраняют ordered immutable refs с version 1. Обычный final text остаётся прежним способом завершения. UI получает `outgoingFiles` в final response. A2A получает text + стандартные FileParts/Artifact parts; adapter не вызывает `str(bytes)` и не повторяет каждое вложение в нескольких местах terminal Message/Artifact.

Перед **первым final frame** загрузить/проверить весь persisted attachment manifest, размеры и digests и применить общий лимит к полному сообщению. Лимит не считается по файлу или SSE frame; URL/reference не обходит его. Уже отправленный progress text не превращается в отправленный final attachment batch. При recovery выдаётся тот же frozen набор, даже если исходные paths удалены или изменены. Integrity/storage error до первого frame даёт safe transport failure без частичных files. Сбой соединения после начала отправки не меняет durable result; повторный GetTask использует прежние IDs/manifest.

Почему выбран этот tool: существующие terminal/Python уже создают любые форматы файлов, но не объявляют, какие нужны пользователю. `core_artifact_save/load/list` создаёт ненужный named/versioned service и подлежит удалению. Новый обязательный final-response tool потребовал бы менять все provider/loop completion branches; `core_response_files` добавляет лишь выбор файлов к уже существующему final text. Выводить attachments из Markdown или имени файла нельзя: это неоднозначно и не даёт проверяемого набора.

### Scoped HTTP/A2A delivery и file management

Owner routes регистрируются до A2A catch-all и используют существующий Keycloak middleware для `/api/`; каждое новое HTTP обращение заново авторизуется. Body/path не содержит trusted owner/tenant. Owner имеет company access, external role не получает owner API даже одновременно с owner role.

Выбранный минимальный API:

| Route | Request/response |
|---|---|
| `GET /api/chats/{context_id}/files` | Bounded preview по принятому public contract: optional `directory`, `older_than_days`, `limit` и `cursor`; `{files,next_cursor,listed_at,active,cleanup_block_reason,workspace_revision}`. Элемент: `name,path,size,mtime_ns,identity_token`. Строго старше N суток, timestamp фиксируется для всех страниц одного preview. |
| `GET /api/chats/{context_id}/files/content?path=...` | Безопасно открывает текущий scoped path через trusted chat fd; не следует symlinks; streaming из открытого fd, bounded headers, safe filename, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`. |
| `GET /api/chats/{context_id}/tasks/{task_id}/files/{file_id}` | Download immutable final attachment только после проверки persisted task/chat manifest и текущего caller access, затем size/digest. Blob ID или digest не является authority. |
| `POST /api/chats/{context_id}/files/delete` | Ровно `{request_id,files:[{path,identity_token}]}` с максимум 1000 уникальных paths; original ordered selection закрепляется в durable intent. Повтор того же запроса возвращает ту же операцию. Completed receipt —200, pending/reconciliation —202; per-file outcomes и totals. Никакого delete-by-age без exact selection. |
| `GET /api/chats/{context_id}/files/delete` | Пассивный receipt lookup по optional request_id либо последняя операция чата для всех владельцев. Исходная selection доступна для явного retry того же намерения. |

`identity_token` version2 — server-computed SHA-256 от binding, scoped relative path, filesystem identity (device/inode/ctime_ns/mtime_ns/size) и реальной chat workspace revision из migration21. Это optimistic precondition, а не bearer authority: server сначала независимо авторизует chat/path, заново получает stat и сравнивает hash. Отдельные preview table и token registry не нужны. Старый v1 token либо stale preview дают skipped/identity_changed; старый cursor отклоняется. Один mtime не защищает от replacement. Client selection сама определяет разрешённый к удалению перечень в owner-authorized запросе.

Preview cursor v1 подписан process-local ключом и связан с company/chat owner,
directory, возрастом, server timestamp и последним относительным путём. Restart
инвалидирует cursor и требует свежего preview; это не durable history cursor.
Traversal ограничен 10000 entries/depth64 и при превышении возвращает явный
WORKSPACE_SCAN_LIMIT409 без частичного успешного списка. Directory filter
позволяет сузить большое дерево. Symlinks/hardlinks/special files и служебные
manifests published attachments не выдаются. Чтение использует nofollow dirfds,
не создаёт отсутствующую папку и не включает binary admission.

Cleanup держит тот же chat row lock, что root admission/cron, и выводит busy из canonical root state. Все non-terminal waits блокируют delete. Изменённый/заменённый/symlink file пропускается, новые файлы не входят в selection, failure части не маскируется общим success. Durable deletion intent/tombstone записывается до unlink; итог каждого пути и новая revision фиксируются после. Recovery завершает только explicit selection при совпадающей identity и не запускает cleanup после нового active root. Snapshots не возвращают удалённые paths. Листинг и download не удаляют ничего и остаются доступны при busy.

Для A2A первоначально использовать inline raw FileParts со standard filename/media type; внешняя download route не нужна для этой формы. Если вводится URL form, она проходит тот же authenticated scope resolver, никогда не является публичным server path и не forward-ит inbound Authorization. Whole-message size check обязателен и для ссылок. Owner UI использует указанные authenticated URLs, не получает filesystem root или private staging paths.

`core_agent_send_message` принимает optional `files: [relative paths]` через ту же output preparation; общий лимит проверяется до первого network attempt, task text вместе с oversized set не отправляется отдельно. `RemoteAgentConnection` сохраняет file parts и aggregate bounds при приёме remote результата, не теряет bytes при `_normalize`/SSE merge и не ретранслирует raw file frames в root stream до полной проверки. Remote result — недоверенный input, поэтому его файлы идут через ту же quarantine/guardrails границу и scope текущего run. Durable remote timeout/replay semantics остаются ответственностью remote-waits среза; локальная подготовка файлов не разрешает blind retry сетевой мутации.

## Выполнение по проверяемым шагам

### 1. Закрепить API/schema и воспроизвести существующие пробелы

**Files:** `spec/tools.md`, `spec/artifacts.md`, `spec/public-contract.md`, `spec/architecture.md`, `spec/agent-configuration.md`, `spec/acceptance.md`, `spec/releases/v1.md`, `tests/test_spec_lock.py`; plan/AGENTS в commit по правилам репозитория.

- [ ] Перед frozen edits подтвердить применимость уже полученной явной директивы; этот документ сам её не создаёт. Зафиксировать core_response_files schema, pending-set errors, file receipts, publication barrier и migration contract в основных spec-документах. Уточнить storage acceptance/publication error distinction, чтобы после committed acceptance transport не обещал rollback.
- [ ] Добавить в существующий unittest suite failing regressions: файл root task исчезает после completion; Python первого вызова получает неверный tenant; новый root того же context видит другую папку; `RunResult` теряет attachment set при serialization/recovery.
- [ ] Выполнить `uv run python -m unittest tests.test_local_terminal tests.test_python_exec tests.test_admission -v`. Убедиться, что failures относятся к новым требованиям, а не broken environment; затем менять runtime.
- [ ] Обновить exact hash bytes. Выполнить `uv run python -m unittest tests.test_spec_quality tests.test_spec_lock -v`. Не переводить новые семантики в implemented до runtime CI proof.

### 2. Ввести trusted workspace binding без binary exposure

**Files:** `core_agent/execution.py`, `core_agent/runtime.py`, `core_agent/tools.py`, `core_agent/python_exec.py`, `core_agent/app.py`, `.env.example`, `docker-compose.yml`; tests `test_local_terminal.py`, `test_python_exec.py`, `test_tasks_tools_execution.py`, `test_postgres_persistence.py`.

- [x] Добавить explicit WorkspaceBinding/resolver и CHAT_WORKSPACE_ROOT wiring. Сначала тесты для двух tenants, двух external owners, owner в external чате, successive root runs и separate chat contexts.
- [x] Перед terminal/Python bind current run из admitted/restored workflow. Перед background dispatch/recovery bind synthetic run ID из persisted contract. Перед child execution bind child run в shared manager; optional scratch проверять отдельным flag/type, не выводить из наличия parent_run_id.
- [x] Удалить fallback scope из execute_transient. Прямые unit callers создают explicit binding; legacy development transport получает свой явно сконструированный development scope и не присваивает его enterprise callers.
- [x] Изменить create/destroy, чтобы persistent workspace переживал task terminal/restart, а cancel чистил только owned процессы. Повторный bind не копирует snapshot и не удаляет чужую session.
- [ ] Проверить restore/outbox recovery path и nested Python tools.call; unknown background ownership безопасно блокирует запуск. Authenticated caller text, tool args, SDK tenant mutation не могут поменять binding.
- [ ] Выполнить `uv run python -m unittest tests.test_local_terminal tests.test_python_exec tests.test_tasks_tools_execution tests.test_postgres_persistence -v` с real PostgreSQL. Зафиксировать один logical commit после sandbox contract review; binary admission остаётся выключенным.

Рабочий срез 30 сентября: trusted binding реализован, spec и code-quality review пройдены. Дополнительный review нашёл и затем подтвердил устранение накопления cached binding после завершения run.
`tests.test_chat_workspaces` проверяет сохранение папки, отсутствие snapshot restore,
root/child/background/recovery scope и owner в external чате; sandbox и binary intake
не подключены. Все root lifecycle terminal paths должны получить подтверждённый teardown
перед освобождением чата в отдельном sandbox integration срезе.

Проверки в текущей ограниченной среде: 46 focused tests workspace/auth/execution после исправления retention, Ruff и 6 spec/hash
проверок прошли. Полный `unittest discover` выполнил 483 tests и завершился с
40 errors / 1 failure / 90 skips: локальные HTTP/Unix listeners запрещены
(`PermissionError: Operation not permitted`), один transport assertion ожидает
connection-refused вместо environment-denied, PostgreSQL/Keycloak URLs отсутствуют.
Python broker, real PostgreSQL и image/Linux gates остаются обязательными;
этот результат не является зелёным release gate и commit не создан.

### 3. Добавить durable private batches и recovery

**Files:** create `core_agent/chat_files.py`; modify `core_agent/database.py`, `core_agent/lifecycle.py`, `core_agent/app.py`, `core_agent/workflow.py`; test `tests/test_transfer_features.py`, `tests/test_postgres_persistence.py`.

- [ ] Добавить migration следующей версии, scoped indexes/grants и checks для допустимых transitions. Existing rows читаются без file manifest. Не выдавать serving role DDL или произвольное удаление chat/history.
- [ ] Реализовать bounded prepare/stage, strict decode, safe names, manifest/fsync и rejection cleanup. Проверить два файла по 13_000_000 → reject целого сообщения, ровно 25_000_000 → допустимый технический batch, empty files, invalid second base64, failure последнего write/fsync и сохранность прежних файлов.
- [ ] Проверить коллизии `report.pdf`, повторное имя внутри одного batch, directory/symlink name, Unicode/control/traversal имена; в receipt/context только actual name. Две concurrent preparations окончательно распределяют имена при chat lock.
- [ ] Добавить durable upload lease и startup/scheduled sweeper. Tests используют controlled clock, не sleep(24h); accepted quarantine и active upload никогда не orphan. Удаление stage failure происходит immediately, restart не обновляет created_at.
- [ ] В реальном PostgreSQL инъецировать process-style restart на каждой стороне commit/rename/fsync/published boundary; повторно создать service/manager на прежнем volume. Assert public files либо отсутствуют, либо полным набором; ни частичного model input, ни второй копии.
- [ ] Выполнить `uv run python -m unittest tests.test_transfer_features tests.test_postgres_persistence -v`. Storage tests не объявляют guardrails или sandbox работающими.

Рабочий storage срез шага 3 реализован в `chat_files.py` и schema 16:
prepare/lease, immutable scoped binding, committed decision, whole-directory
no-replace publication/recovery и bounded sweeper. Полный manifest проверяется
по составу каталога, regular-file identity, длине и digest. Bind ошибки очищают
непринятый stage; после rollback внешней admission transaction caller обязан
вызвать reject уже вне неё. После аварии действует original-age sweep.
Rowless uploads очищаются только по сохранённому возрасту после проверки batch,
run и wait references; quarantine и неизвестная/повреждённая metadata сохраняются.

Spec/code review пройдены. Последние проверки storage: 30 tests, exit 0,
14 PostgreSQL skips; Ruff/diff-check прошли. Review устранил утечки дескрипторов
при fsync/stat errors и неистекающие lease при NaN/Infinity. Реальные PostgreSQL
rollback/concurrency/process-crash и Linux mounted-inode hostile race proofs
здесь не выполнены. Поэтому step 3 не объявляется полностью закрытым gate.

Service теперь создаётся в enterprise app до recovery и закрывается при shutdown.
Root/follow-up admission transaction и startup/hourly sweep подключены; review
подтвердил коррекцию PostgreSQL pool usage. Public binary input остаётся
отклонённым до обязательных native proofs.

Runtime publication barrier и private owner review прошли отдельное review:
каждый файл проверяется вместе с именами/metadata immutable manifest, а публикация
разрешается только после положительного решения для всего batch. Reject/timeout
одного файла исключает весь batch; отдельно проверенный текст продолжает задачу.
Неподдерживаемый MIME, пустой текст и неполная extraction требуют решения владельца.
Повторное получение отклонённых точных UTF-8 данных как inline text тоже запрещено,
включая unsupported MIME; byte identity не смешивается с canonical JSON identity.

Owner API `/api/guardrails/{wait_id}/material` показывает manifest/исходное имя,
`/file` выдаёт проверенные bytes только после canonical scope lookup. Company
setting `attachment_limit_bytes` (schema 18) по умолчанию равен 25 000 000;
старый PUT трёх таймаутов сохраняет лимит, изменение не переоценивает уже принятый
immutable manifest. Service prepare принимает trusted per-message limit.

Последний review gate runtime file/inline guards + stores: 117 tests, exit 0,
75 executed, 42 PostgreSQL skips. Owner material/file/settings focused gate:
34 tests, exit 0, 23 executed, 11 PostgreSQL skips. Эти результаты не заменяют
ещё не выполненные PostgreSQL и Linux mounted-inode hostile race gates.

### 4. Связать root/follow-up, реальные decisions и publication barrier

**Files:** `core_agent/admission.py`, `core_agent/a2a_sdk.py`, `core_agent/a2a.py`, `core_agent/app.py`, `core_agent/runtime.py`, `core_agent/workflow.py`, `core_agent/chat_files.py`; tests `test_admission.py`, `test_auth.py`, `test_end_to_end.py`, `test_postgres_persistence.py`.

Рабочий admission срез: server-owned initial/follow-up batch pointers, root
rollback и синхронный memory commit после async locks, canonical duplicate/busy
и cancel ordering, real sweep и durable publication-pending recovery реализованы.
Review обнаружил nested PostgreSQL pool acquisition; two-pass preflight/staging/
commit устраняет его без удержания outer connection во время prepare. Отдельно
проверяется loser stage cleanup после transaction и сохранение original age при
ошибке cleanup. Два concurrent private stage допустимы, accepted/published один.

После исправления portable focused gate: 134 tests, 71 executed, 63 PostgreSQL
skips, exit 0. Новые real PostgreSQL scenarios для pool_max=1, concurrent duplicate
stages и cancel между preflight/bind включены в suite, но здесь не выполнены.
Required admission/auth/E2E/PostgreSQL module gate встретил `socket.bind: EPERM`
в HTTP server fixture; это блокированный gate, не PASS. До фикса pool более
широкий portable срез проходил 271 tests (139 executed, 132 PostgreSQL skips).

Финальное независимое review admission исправлений: блокеров не осталось.
Gate file_admission/admission/auth/runtime_file_guardrails/chat_files: 152 tests,
81 executed, 71 PostgreSQL skips, exit 0. Проверка кода не подменяет реальные
PostgreSQL rollback/concurrency и Linux native isolation proofs.

Последующее review terminal lifecycle выявило ранний отказ follow-up при
`terminal_intent`. Исправлено в общем memory/PostgreSQL inbox и file bind:
nonterminal input принимается до terminal commit, публикация всё ещё закрыта.
При success Task открывает новую execution generation; при failure входы
сохраняются с disposition. Gate terminal_barrier/file_admission/durable_waits
и MCP startup failure:72 tests,47 executed,25 PostgreSQL skips,exit0;
независимые три regression scenarios прошли без пропусков.

- [ ] Сначала подключить результат durable-interactions/guardrails и sandbox срезов: owner role policy, deny-wins external role, persisted allow/reject/timeout/detector failure, реальное уведомление владельца и recovery. При отсутствии любого обязательного пути enterprise binary input продолжает fail-closed.
- [ ] Root: prepare complete stage → admit с file_batch binding в общей transaction → no model/MCP until delivery ready. Duplicate fingerprint возвращает original Task/receipts без второго classifier/model, accepted batch или опубликованной копии. Changed bytes/name/context при прежнем messageId даёт conflict.
- [ ] PostgreSQL admission сначала проверяет duplicate/busy/scope под canonical
  locks, затем освобождает connection до независимых settings/staging transactions.
  После prepare новая admission transaction повторяет все проверки и bind.
  Между двумя concurrent первыми попытками допустимы private временные stages;
  проигравший удаляется после выхода/rollback transaction, а crash cleanup
  использует прежние lease/age. Эти bytes не публикуются и не классифицируются.
  Это тот же контракт, что algorithm п.3 выше, а не гарантия отсутствия любых
  temporary disk writes. Valid pool size 1 не должен требовать второе connection.
- [ ] Follow-up: убрать old store_attachments и связать staged batch с existing durable inbox transaction. При terminal/cancel race одно решение: accepted ordered input либо отказ без publication. Ошибка второго файла не меняет unread inbox/transcript и не прерывает активный model/tool call.
- [ ] Одновременно обновить live worker и `_recover_workflows_once`/continue path: publication barrier проходит до model setup/turn; unread pending files запрещают normal completion. Owner guard reject исключает материал и продолжает по GUARD semantics, не доставляет его по старому snapshot.
- [ ] Запустить на PostgreSQL конкурентные HTTP requests отдельными connections: same root messageId/new tokens → одна Task/один batch; разные root IDs одного context → одна admitted + failed busy; same follow-up ID → один inbox batch; different chats progressing independently. Во время blocked terminal процесса rejected upload не становится видимым ни на миг.
- [ ] Проверить root text+files, files-only и follow-up text+files через обе authenticated A2A routes; внешнему B неизвестны files/chat A, owner сохраняет external execution owner. URL input по-прежнему отказан без trusted resolver.
- [ ] Выполнить `uv run python -m unittest tests.test_admission tests.test_auth tests.test_end_to_end tests.test_postgres_persistence -v`. Только после прохода real decision и hostile sandbox gates убрать temporary enterprise binary rejection.

Проверенный срез 1 октября 2026: mandatory native ARM64 Kubernetes sandbox
прошёл все 12 тестов без skips; после этого открыт приём inline raw Parts на
owner/external HTTP+JSON и JSON-RPC Send/SendStreaming routes. URL Parts остаются
отклонёнными. Pre-SDK guard проверяет encoded body, JSON и canonical base64 до
admission. Targeted ingress/admission/auth/file/guardrail/transfer suite прошёл
308 тестов с реальным PostgreSQL без skips. Owner history показывает только
опубликованные безопасные attachment receipts; 38 history tests прошли на
PostgreSQL. UI multiple-file picker и immutable retry проверены 11 сценариями
в Chromium с контролируемым API. Затем actual Chromium + Keycloak + PostgreSQL +
native Linux application Pod прошли required module без skips: 20 browser checks,
включая lost-ACK retry, HITL, publication, persisted history/download и storage
обеих вкладок. Native AMD64 и целевой CSI остаются отдельными gates.
Эти результаты не закрывают исходящие files и migration/cutover следующего шага.

### 5. Реализовать final-file selection и полный A2A/remote transport

**Files:** `core_agent/app.py`, `core_agent/runtime.py`, `core_agent/config.py`, `core_agent/kernel.py`, `core_agent/a2a.py`, `core_agent/a2a_sdk.py`, `core_agent/artifacts.py`, `core_agent/remote_agents.py`, `core_agent/chat_files.py`; tests `test_transfer_features.py`, `test_python_exec.py`, `test_end_to_end.py`, `test_runtime_observability.py`.

- [x] Зарегистрировать core_response_files, schema и concise kernel instruction; capability intersection/delegation/deny/policy действуют как для остальных tools. Не добавлять их в RunRequest или provider-specific response format.
- [x] Snapshot весь новый набор и только затем заменить manifest вместе с tool receipt. Assert invalid path/oversize/read error оставляет прежний set и source files, `[]` clears, две успешные команды заменяют set целиком. Проверить recovery после receipt и до final result.
- [x] Добавить outgoing_files в RunResult и все result reconstruction branches. Budget-partial result с выбранными файлами сохраняет прежние complete/completion_reason/shared_budget; attachment bytes не попадают в telemetry.
- [x] Обновить result_artifact/_publish_artifact, чтобы raw FileParts сохраняли media type/name/bytes и весь batch проверялся до первого final frame. GetTask/live/SSE/recovery одинаковы по IDs, digests и ordered files; stream disconnect не дублирует результат.
- [ ] Добавить same-tenant different-chat blob isolation test, включая одинаковые file bytes: известный digest/file_id без доступной Task manifest не даёт download. In-memory и PostgreSQL проходят один observable contract.
- [x] Remote outbound: optional per-call `files`, общий snapshot/limit/scope service до HITL, безопасные approval receipts и повторная whole-manifest validation до Send. Отсутствие `files`/`[]` не наследует workspace или final selection; oversized/invalid batch не создаёт handle и первый HTTP request. Incoming headers не становятся downstream Authorization.
- [ ] Remote inbound completed batch проходит atomic quarantine/claim/source-root binding и guardrails до workspace/model publication; transport whole-response bounds исключают relay частичного превышающего лимит материала.
- [x] Выполнить `uv run python -m unittest tests.test_transfer_features tests.test_python_exec tests.test_end_to_end tests.test_runtime_observability -v`.

Backend итоговых вложений и source UI подключены: immutable snapshots, whole manifest
validation, ordered typed Parts, owner final history и отдельный authenticated
download. Проверены direct/Python selection, HITL, guardrail wait/recovery и
budget-partial, реальные PostgreSQL save/restart и pool1 reads. Review выявило
и исправило null manifest, colon basename, cached/equal-version save shortcuts
и позднее подключение сервиса при startup reconciliation. Full PostgreSQL +
Keycloak suite —1465 tests, 208.608 секунды, exit0, три ожидаемых skips; отдельный
card gate подтвердил binary output mode на owner/external endpoints. Ruff и UI
typecheck/build проходят. Extended actual browser output/download proof прошёл
на native ARM64 application Pod с реальными Keycloak/PostgreSQL: required module
1 test, 79.291 секунды, exit0, без skips, 28 browser checks. UI скачал два выбранных
snapshots, включая empty file, после изменения/удаления originals и позднего
уменьшения company limit. Проверены current bearer/encoded route, per-file busy,
abort при смене чата и отсутствие stale download; incoming checks сохранены.
Image ID: `sha256:d785ee4fa13f38dbd7d3bb43d0a88d9ff93418e550c213d5a99c761c3eb20a8d`.
Evidence — `.local-evidence/owner-browser-outgoing/`; source hashes включают
последний binary AgentCard output mode. Remote inbound files, migration/cutover и полный
release этим не закрыты; actual CI AMD64 и целевой CSI остаются отдельными gates.
Финальный набор выбирает модель через `core_response_files`; остальные файлы
workspace не прикрепляются автоматически. Вложения при создании задачи внешнему
агенту теперь выбираются отдельным `files` на каждый вызов. Schema доступна
только enterprise file runtime; legacy catalog остаётся text-only. Job v2 хранит
source scope, pinned aggregate limit и immutable refs; прежний v1 читается без
изменений. Snapshot/arguments digest переживают HITL и runtime restoration,
изменение originals или settings не меняет approved bytes. Memory и PostgreSQL
root/Python runtime gate —14 tests, exit0, без skips; executor/scheduler gate —
68 tests, exit0, без skips; actual wire transport gate —45 tests, exit0, без skips.
Проверены обе bindings, empty binary files, pre-dispatch aggregate refusal,
strict pre-SDK validation и отсутствие повторного Send после malformed redirect.
Remote working/input-required/auth-required raw previews сохраняют IDs/deadline
и polling без relay/import. Child scope tests использовали PostgreSQL jobs/blobs
и in-memory workflow: настоящий delegated-child PostgreSQL file pipeline ещё
требует отдельного proof. Completed remote files пока явно unsupported, до
atomic inbound quarantine/guardrail среза. Evidence —
`.local-evidence/remote-outbound/`, `.local-evidence/remote-transport/` и
`.local-evidence/remote-files-runtime-final.log`.
Финальный полный прогон после redirect/preview исправлений на свежей PostgreSQL
БД и настоящем Keycloak: 1501 tests, 196.434 секунды, exit0, три ожидаемых skips;
Ruff и diff-check прошли. Logs/exit — `.local-evidence/remote-outbound-final.*`.

### 6. Подключить owner file API и безопасную очистку

**Files:** `core_agent/workspace.py`, `core_agent/workspace_cleanup.py`, `core_agent/admission.py`, `core_agent/owner_api.py`, `core_agent/chat_files.py`, `core_agent/database.py`; composition/recovery `core_agent/app.py`, `core_agent/runtime.py`, SDK error binding `core_agent/a2a_sdk.py`; UI `WorkspaceFiles.tsx`, `Chat.tsx`, `styles.css`; tests `test_workspace_files.py`, `test_workspace_cleanup.py`, `test_workspace_cleanup_api.py` и существующие auth/admission/persistence suites.

- [x] Read-only preview/current download: точный контракт принят в public contract
  и FILE-04; routes подключены через существующий authenticated owner_routes.
  Memory/PG canonical scope сохраняет original owner, nofollow dirfds исключают
  небезопасные targets, cursors подписаны и сохраняют server age timestamp,
  listing/download не создают папку и не запускают работу. API доступен при waits;
  binary admission этим не включается.

- [ ] Внести точные route schemas из этого плана в public contract прежде runtime. Добавить list/current download/final attachment download; не вводить общий unauthenticated static-file mount.
- [ ] Проверить authorization на каждом request, owner list external чата, external-role запрет `/api`, revoked token при следующем download, symlink/path escape и corrupted blob. Байтам при download назначать безопасный Content-Disposition, не доверять filename заголовкам.
- [x] Реализовать preview exact selection и schema-versioned deletion intent/receipts;
  N-day filter использует один server timestamp и строгую границу. Empty selection
  ничего не удаляет; replaced file skipped, partial errors перечислены. Schema21,
  committed intent до capture, content/parent proofs, private fsynced journal и
  оба directory fsync перед terminal receipt; malformed persisted protocol
  сохраняет admission barrier. Root admission и cleanup используют canonical lock.
- [x] Подключить service в enterprise composition до startup recovery;
  существующий coordinator делает один bounded fair cleanup pass на tick.
  GET receipt не вызывает recovery. HTTP send/stream и JSON-RPC возвращают safe
  retryable отказ до root admission; duplicate accepted Task не меняется.
- [x] Подключить source UI: ручной выбор максимум 1000 файлов, отдельное
  подтверждение всех names/paths/bytes, неизменные request ID/тело для явного
  retry, per-file outcomes, latest receipt после reopening и dirty guards при
  uncertain операции. Ни refresh, ни filter, ни возраст не создают POST.
- [ ] На PostgreSQL состязать cleanup с root admission и simulated cron admission через тот же service: нет удаления при active root/любом wait. Проверить restart после unlink до final DB result, отсутствие resurrection и сохранность полной истории.
- [ ] Согласовать JSON с owner UI plan: обычные final attachments отображаются и скачиваются; preview не выбирает файлы автоматически; busy explanation и per-file outcome видны. ENT-AC-58–61 не закрывать одним API-тестом до UI proof.
- [ ] Выполнить `uv run python -m unittest tests.test_auth tests.test_admission tests.test_transfer_features tests.test_postgres_persistence -v`.

Проверенный preview/download срез: focused workspace/owner/history/admission/
files/interactions gate224 tests,109 executed,115 PostgreSQL skips,exit0.
Дополнительные независимые ASGI/filesystem проверки подтвердили forged cursor
и duplicate-query отказ, реальный 10001-entry cap, отключение unsafe files и
descriptor cleanup при disconnect/cancellation. Spec/security и quality review
не нашли блокеров. UI source читает только bounded pages, показывает метаданные,
server timestamp и явные folder/age filters; stale responses не меняют новый
просмотр. Cleanup подключён отдельным проверенным срезом: независимый combined
gate333 tests,146 executed,187 PostgreSQL skips,exit0. Дополнительный HTTP gate
178 tests,85 executed,93 PostgreSQL skips,exit0; actual source Node helpers
проверяют immutable retry, empty receipt и partial outcomes. Review исправил
recovered-source fsync, corrupt journal, fairness, terminal receipt coherence,
proof validation и SDK JSON-RPC error model. Final attachment download и binary
ingress/egress остаются открытыми. Real PostgreSQL migration/locks/grants,
power-loss proof и browser gates этим не доказаны.

### 7. Миграция старых файлов, удаление dedicated artifact service и release gates

**Files:** `core_agent/artifact_service.py`, `core_agent/app.py`, `core_agent/runtime.py`, `core_agent/config.py`, `core_agent/kernel.py`, `core_agent/database.py`, `pyproject.toml`, `uv.lock`, `.env.example`, `README.md`, `AGENTS.md`; соответствующие существующие tests/spec/status.

- [ ] До удаления экспортировать inventory старых names/versions/digests/ownership и описать explicit mapping в целевой tenant/owner/context. Не угадывать caller из anonymous, user name, токена или последнего запроса. Неоднозначные blobs остаются закрытыми для external, сохранёнными для авторизованного export/mapping.
- [ ] Migration job проверяет source integrity и target manifest и публикует целую revision через тот же durable storage protocol. Повторный запуск idempotent; сбой не меняет source и прежнюю опубликованную revision. User-scope blobs без chat mapping сохраняются в export archive, не привязываются ко всем чатам.
- [ ] После замены inbound flow удалить core_artifact_save/load/list, registry/schema/handlers/instructions, dedicated S3/Mongo backends и только их dependencies/env config. Сначала проверить callers через rg: transport `artifacts.py`, offload, push blobs и WorkspaceSnapshotStore сохраняются. Invalid legacy env даёт actionable upgrade diagnostic, не молчаливый выбор старого backend.
- [ ] Existing frozen tests старых artifact tools заменить в разрешённом change на проверки файлового результата и сохранности migration source; не ослаблять shared transport/snapshot assertions. `AGENTS.md` описывает реально подключённые tools и file flow; статус requirement обновляется только с CI proof.
- [ ] Deployment: остановить writers → backup согласованных DB+POSIX volumes → отдельная migration job → новый image с требуемой schema → recovery/quarantine reconciliation → открыть ingress. Serving process только проверяет schema. После accepted batch/новой workspace revision откат старого image без DB+volume rollback запрещён: он не знает publication barrier и может потерять input или resurrect данные. Backup согласуется с file manifests, а не отдельно с DB timestamp.
- [ ] Выполнить `uv run ruff check core_agent tests`; `uv run python -m unittest tests.test_spec_quality tests.test_spec_lock -v`; затем `uv run python -m unittest discover -s tests -v` с PostgreSQL/pgvector и Keycloak так же, как `.github/workflows/ci.yml`. Применимые container/sandbox gates выполнить на Linux с реальным Bubblewrap. Skipped PostgreSQL/Keycloak/sandbox не является proof соответствующих требований.
- [ ] Выполнить `git diff --check`. Зафиксировать logical commits по завершённым срезам; source manifests и old user blobs не удалять ради сокращения diff. Обновить основной enterprise plan фактическим порядком и подтверждёнными результатами.

## Границы готовности

План требует настоящих durable guardrails/HITL и sandbox до открытия executable binary ingress. Он не предлагает их имитацию и не заявляет весь enterprise RC реализованным. Workspace binding/private storage могут быть закончены и проверены раньше; REST/UI display, remote waits и cluster sandbox имеют собственные обязательные end-to-end gates. Это зависимости порядка реализации, а не новые продуктовые вопросы.
