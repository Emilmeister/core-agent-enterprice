# План: уточнения внешних A2A-агентов и артефакты в workspace

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task after user approval. Steps use checkbox syntax. Keep changes in a permanent repository or worktree; use existing unittest, PostgreSQL and native browser gates.

**Статус:** для согласования. Изменения поведения, кода и нормативной спецификации ещё не выполнены.

**Goal:** Наш агент отвечает на публичные уточнения внешнего агента в рамках исходной A2A-задачи; входящие A2A-файлы и содержимое полученных артефактов сохраняются в каталоге `a2a/` workspace чата и не загружаются автоматически в контекст модели.

**Architecture:** Расширяем существующие remote executor, scheduler, workflow waits и файловый pipeline. Уточнения и ответы сохраняются durable с привязкой к исходной операции. Артефакты проходят существующие quarantine, guardrails и atomic publication; модель получает только проверенные метаданные и локальные пути.

**Tech Stack:** Python/uv, существующие in-memory и PostgreSQL stores, A2A 1.0 JSONRPC и HTTP+JSON, React/TypeScript, существующие файловые API и браузерные проверки. Новые сервисы и зависимости не требуются.

## 1. Согласованное поведение

### 1.1. Уточнения от внешнего агента

- `input-required` с публичным вопросом к вызывающему агенту становится событием, которое получает наш агент.
- Вопрос сохраняется до пробуждения. Спящий на `core_task_wait` агент просыпается; занятому агенту вопрос доставляется на безопасной границе, после текущего model/tool call.
- Наш агент отвечает из доступного контекста либо задаёт владельцам вопрос через существующий `core_ask_owner`. Внутренняя переписка с владельцами остаётся закрытой; наружу отправляется только ответ, необходимый для исходного поручения.
- Ответ продолжает исходную удалённую Task с закреплёнными `taskId` и `contextId`. Новая удалённая Task и новый локальный handle не создаются.
- Ожидание чужого HITL или авторизации продолжает polling. Наш агент не получает право разрешать чужие действия.
- Новый вопрос доставляется один раз. Повторный GetTask snapshot не будит модель повторно. Отдельный вопрос с новым идентификатором обрабатывается отдельно, даже если его текст совпадает.
- Ответ не продлевает исходный deadline, не обнуляет возраст polling и не создаёт новый model/tool budget. Сохраняются действующие интервалы 10/30/300 секунд и 15-секундный режим открытой панели.
- Вопросы, ответы, признаки отправки и состояние ожидания переживают перезапуск. Неопределённый исход отправки требует сверки; автоматический повтор мутации запрещён.

### 1.2. Различение вопроса к нам и ожидания владельца

Для наших агентов добавить безопасную публичную metadata:

```json
{"core_agent_wait":{"version":1,"kind":"owner"}}
```

- Маркер формируется backend из реального ожидания owner input/HITL/guardrails, а не из текста модели. Он сообщает только причину ожидания, без вопроса, ответа, аргументов или внутренних идентификаторов.
- `input-required` с этим маркером означает ожидание владельца удалённого агента. Наш агент продолжает ждать.
- У стороннего агента без маркера непустой публичный `status.message` в состоянии `input-required` трактуется как запрос информации вызывающему клиенту согласно A2A. Перед моделью он проходит проверки недоверенного материала.
- Если публичного вопроса нет либо содержимое помечено private/reasoning, модель не получает вопрос; показывается только состояние ожидания.
- Маркер не является подтверждением действия, не меняет роли, права доступа или policy. `auth-required` не запускает автоматическую отправку credentials.
- Обновить одинаково Get/List, streams и push projection. Наши старые ожидания после обновления получают безопасный маркер при построении публичной проекции, без раскрытия сохранённой переписки.

### 1.3. Инструмент продолжения внешней задачи

Расширить существующий `core_agent_send_message`, добавив пару необязательных полей `task_id` и `input_id`. В режиме ответа оба поля обязательны; при первоначальной отправке оба отсутствуют.

Первоначальная отправка:

```json
{"agent_name":"weather-agent","task":"Узнай погоду","files":[]}
```

Ответ на уточнение:

```json
{"agent_name":"weather-agent","task_id":"local-operation-1","input_id":"local-input-1","task":"Для Казани","files":[]}
```

- `task_id` — локальный handle, уже известный модели; `input_id` — backend-issued идентификатор текущего запроса. Модель не передаёт внешние идентификаторы или URL.
- Backend проверяет tenant, owning run, исходного адресата, актуальность вопроса, срок, cancel/terminal state и закреплённую конфигурацию. Чужой handle имеет not-found semantics; устаревший `input_id` возвращает структурированную ошибку без отправки.
- Ответ проходит schema, текущую tool policy, отдельный HITL при необходимости и guardrails. `files` по-прежнему выбираются явно и snapshot-ятся до approval.
- Результат инструмента различает принятие ответа в durable очередь и подтверждённую отправку. После принятия модель может снова вызвать `core_task_wait` для той же операции.
- Нельзя использовать эту пару полей для произвольного сообщения в чужую Task или для возобновления истёкшей операции.

### 1.4. Единый каталог для A2A-файлов и артефактов

Сохранять проверенные A2A-файлы и артефакты в отдельный каталог внутри workspace данного чата. Пример результата внешней операции:

```text
/workspace/a2a/<local-operation-id>/
    manifest.json
    artifact-001/
        part-001.md
        part-002.json
    artifact-002/
        diagram.png
```

- Под `a2a/` сохраняются все файловые вложения, полученные через A2A: самостоятельные FileParts в Message, файловые части Artifact и файлы во входящем обращении к нашему A2A endpoint. Upload через UI сохраняет существующее расположение в `attachments/`.
- Ответы внешних агентов группируются по локальной операции. Вложения исходного входящего A2A обращения используют backend-issued локальный batch ID. Manifest связывает каталог с канонической Task/операцией; входящие remote IDs не становятся именами папок.
- Каталог и служебные имена формируются backend. Remote IDs, названия и пути не используются напрямую как filesystem paths.
- Текстовые части сохраняются в UTF-8; `text/markdown` получает `.md`, прочий текст — `.txt`. Части `data` сохраняются как JSON, бинарные части — как обычные входящие файлы.
- `manifest.json` сохраняет исходный `artifactId`, название и порядок артефактов/частей, тип каждой части, локальный путь, размер и SHA-256. Содержимое частей хранится в отдельных файлах; скрытые поля и reasoning исключаются.
- Результат без бинарных файлов, содержащий только текстовый Artifact или `data`, проходит тот же admission/publication pipeline. Смешанный Artifact не теряет связь между текстом, JSON и файлами.
- Структурированная часть непосредственного A2A Message также материализуется, с сохранением происхождения Message; протокольный `artifactId` для неё не выдумывается.
- В ответах внешних агентов только успешный terminal Task/Message публикует содержимое в workspace. Промежуточные артефакты не открывают ранние файлы и не обходят существующий барьер финального результата. Вложения входящего поручения публикуются после обычных admission/guardrails, до выполнения поручения: их не требуется ждать до завершения нашей Task.
- Повторное получение результата и recovery используют сохранённый batch. Нет повторной материализации, скрытой перезаписи или повторной передачи модели уже отклонённого материала.

### 1.5. Что получает модель и UI

- `core_task_wait`, `core_task_get`, `core_task_list`, mailbox и tool results передают статус операции, перечень артефактов/частей, имена, типы, размеры и пути. Содержимое Artifact не добавляется в `text`, summary или служебные подсказки автоматически.
- Модель читает выбранные файлы через Python/терминал, когда это необходимо. Большой результат не заполняет контекст только из-за завершения внешней операции.
- Публичные Messages, уточнения и ответы остаются сообщениями. Текст внутри Artifact является файлом, даже если внешний агент использовал его для своего окончательного ответа.
- В UI сохраняется переписка по контрагентам: вопрос, ответ, состояние и карточки артефактов. Контент доступен через существующие открытие, скачивание и предпросмотр.
- Файлы из `a2a/` участвуют в обычной очистке workspace. Удалённые файлы отмечаются недоступными; сохранённая информация о результате не обещает их повторного скачивания.
- Полученные артефакты не становятся исходящими вложениями автоматически. Модель явно выбирает файлы для новой отправки или итогового ответа.

### 1.6. Лимиты и проверки

- Использовать закреплённый company `attachment_limit_bytes`: суммарно учитывать бинарные файлы, UTF-8 текст, сериализованный JSON и создаваемый manifest. Отдельно сохраняется существующий encoded transport ceiling.
- Ограничить количество артефактов/частей и глубину JSON существующими transport/staging bounds; oversized результат получает явную ошибку, без молчаливой потери частей.
- Guardrails проверяет полный материал до публикации, включая текст и JSON. Отклонение или timeout не открывают содержимое через иной API/путь и позволяют нашему агенту продолжить без результата.
- Сохранить защиту от отражённых credentials, path traversal, symlinks, cross-chat access, недействительных scope и подмены batch.
- Файлы по URL не скачивать автоматически в этом изменении. Существующее ограничение остаётся явным; поддержка `data` его не ослабляет.

## 2. Карта изменений

| Область | Файлы и назначение |
| --- | --- |
| Нормативные требования | `spec/tasks-and-delegation.md`, `spec/tools.md`, `spec/a2a-protocol.md`, `spec/artifacts.md`, `spec/public-contract.md`, `spec/runtime.md`; критерии в `spec/acceptance.md`, поставка в `spec/releases/v1.md` |
| Парсинг A2A | `core_agent/remote_agents.py`: сохранить public question, отдельно Messages и Artifacts, их идентичность и typed parts |
| Remote operation | `core_agent/remote_operations.py`: durable вопросы/ответы, отправка продолжения, materialization terminal artifacts |
| Scheduler и persistence | `core_agent/tasks.py`, `core_agent/postgres_tasks.py`, `core_agent/workflow.py`, `core_agent/database.py`: одинаковые fenced переходы, versioned checkpoints, mailbox/waits, backward compatibility |
| Agent loop и tools | `core_agent/runtime.py`, `core_agent/app.py`: schema и policy продолжения, safe-boundary delivery, ожидание, file-only model projection |
| Файлы и проверки | `core_agent/chat_files.py`, `core_agent/material_reviews.py`, `core_agent/workspace.py`, `core_agent/workspace_cleanup.py`: существующий quarantine/publication pipeline с backend-owned целевым каталогом |
| Owner/Public projection и A2A input | `core_agent/peer_conversations.py`, `core_agent/owner_api.py`, `core_agent/a2a_sdk.py`: вопросы/ответы и артефакты в разрешённой проекции, безопасная причина ожидания, размещение входящих A2A-вложений в `a2a/` |
| UI | `ui/src/PeerConversations.tsx`, `ui/src/types.ts` и существующие file/history components: сообщения и карточки артефактов, предпросмотр по запросу |
| Документация и доказательства | `README.md`, `AGENTS.md`, `spec/implementation-status.md`, `tests/test_spec_lock.py` — только после соответствующих изменений и проверок |

Править shared-код в общей точке и сохранить одинаковое поведение прямых вызовов, nested Python, in-memory и PostgreSQL. Не создавать второй scheduler, отдельный artifact storage service или новые artifact-tools.

## 3. Последовательность реализации

### Этап 1. Нормативный контракт и compatibility

- [ ] После согласования обновить требования перечисленных spec-документов и добавить acceptance для обоих изменений. Прежнее утверждение «любой remote input-required только ожидает владельца» заменить указанным различением.
- [ ] В требованиях определить формы public question, safe wait marker, reply arguments и artifact receipts; привести JSON-примеры из раздела 1.
- [ ] Зафиксировать новый remote contract v3 и checkpoint v2 для новых операций. Сохранить строгие readers прежних contract v1/v2 и checkpoint v1; не переписывать их автоматически.
- [ ] Новые операции получают новое поведение; старые принятые операции и завершённые результаты сохраняют прежнюю семантику. Старые результаты не материализуются повторно при обычном чтении.
- [ ] Использовать существующие JSON-поля для checkpoint/reply/artifact metadata. Если изменения persisted batch требуют SQL, добавить одну явную additive migration через `core_agent/database.py`; сохранить defaults старых batch и порядок migration job перед новым agent image.
- [ ] Обновить frozen spec hashes, сохранив проверку точных bytes. Не помечать реализацию готовой до прохождения CI proof.

Проверка: `uv run python -m unittest tests.test_spec_quality tests.test_spec_lock -v` — exit 0.

### Этап 2. Парсинг вопросов и typed artifacts

- [ ] Добавить случаи в `tests/test_remote_transport.py`, `tests/test_peer_conversations.py`: public question, owner marker, пустой вопрос, private/reasoning, одинаковый текст с разными message IDs, mixed Artifact, JSON-only Task и Message.
- [ ] Запустить новые случаи до изменения parser и подтвердить отсутствие нужного поведения.
- [ ] Расширить `RemoteEvent` дополнительными полями с defaults, не сломав существующие positional callers. Сохранять идентичность контейнера и порядок Parts; не склеивать Artifact text в публичный Message.
- [ ] Сохранить strict JSON/base64, конечные числа, transport limits, фильтрацию private metadata и редактирование отражённых секретов.
- [ ] Проверить обе bindings и отсутствие изменения terminality: `input-required` не является завершением, окончание Artifact chunk не завершает Task.

Проверка: `uv run python -m unittest tests.test_remote_transport tests.test_peer_conversations -v` — новые случаи PASS; PostgreSQL cases должны выполняться с реальным `TEST_DATABASE_URL`.

### Этап 3. Durable доставка уточнения модели

- [ ] Расширить `tests/test_remote_operations.py`, `tests/test_remote_runtime.py`, `tests/test_python_waits.py`: вопрос при sleep и при независимой работе, повторный snapshot, несколько операций/контрагентов, новый вопрос с прежним текстом, current-call boundary.
- [ ] Сохранить вопрос, его идентификатор и признак доставки атомарно с remote checkpoint и уведомлением. Использовать существующие claims/leases и mailbox; stale worker не создаёт вторую доставку.
- [ ] На текущем `core_task_wait` возвращать промежуточный tool result с `task_id`, `input_id`, `question` и `remote_state`. Remote operation остаётся нетерминальной; terminal wait после ответа использует тот же handle.
- [ ] Вопрос вне текущего wait доставлять как отдельное событие от внешнего агента на safe boundary. Он не становится командой владельца и не меняет EffectiveConfig.
- [ ] Проверить новое ожидание через nested Python: доставка вопроса не позволяет продолжить remainder скрипта до завершения предусмотренной continuation.
- [ ] Проверить restart до доставки, после доставки и во время `core_ask_owner`; каждое состояние восстанавливает ожидаемое продолжение без повторного вопроса модели.

Наблюдаемый критерий в runtime-проверке:

```python
assert outcome["task_id"] == original_local_handle
assert outcome["remote_state"] == "TASK_STATE_INPUT_REQUIRED"
assert outcome["input_id"] == persisted_input_id
assert outcome["question"] == "Для какого города?"
assert operation.state not in {"completed", "failed", "canceled"}
```

Проверка: `uv run python -m unittest tests.test_remote_operations tests.test_remote_runtime tests.test_python_waits -v` — exit 0 с реальным PostgreSQL.

### Этап 4. Ответ в исходную внешнюю Task

- [ ] Добавить проверки продолжения и schema в existing remote/runtime suites: только одна из пары полей, чужой handle, неверный адресат, устаревший вопрос, отсутствие pending question, terminal/cancel/expired, tool deny, owner approval.
- [ ] Расширить schema `core_agent_send_message` и закрепление вызова до HITL. Reply сохраняет исходного адресата/revision и отдельный immutable digest ответа/вложений.
- [ ] Атомарно принять ответ в существующую remote operation. Перед сетевой мутацией сохранить уникальный message ID и send intent. Worker отправляет Message с исходными remote IDs через тот же pinned connection.
- [ ] Отдельно сохранять `queued`, `confirmed` и неизвестный исход ответа. Повторное применение того же workflow tool call возвращает прежний admission; не отправляет второй Message.
- [ ] При доказанной pre-dispatch ошибке вернуть исправляемую tool error. При неизвестном side effect вернуть `SIDE_EFFECT_UNKNOWN`, сохранить контекст для сверки и не переслать автоматически.
- [ ] После ответа продолжить polling с исходным deadline. При следующем уточнении повторить цикл; model/tool budgets ограничивают число ответов.
- [ ] Протестировать реальные JSONRPC/HTTP+JSON requests на контролируемом test peer и восстановление через новый PostgreSQL pool. Не отправлять эти проверки реальным подключённым агентам.

Проверка: повторить remote transport/operation/runtime suites и `tests.test_interactions`; ожидается один первоначальный Send и один подтверждённый Send на каждый принятый ответ, без повторов после restart.

### Этап 5. Материализация артефактов через файловый pipeline

- [ ] Расширить `tests/test_remote_inbound_files.py`, `tests/test_remote_file_results_runtime.py`, `tests/test_workspace_files.py`: text-only Artifact, JSON-only, mixed parts, самостоятельный FilePart в Message, исходное входящее A2A-вложение, совпадающие/опасные имена, empty files, несколько артефактов, aggregate limits.
- [ ] Преобразовать разрешённые Artifact parts в staged files и manifest. JSON сериализовать детерминированно с `allow_nan=False`; фиксировать точные UTF-8 bytes и SHA-256.
- [ ] Расширить existing `ChatFileService` внутренним backend-owned назначением каталога. Старые batches и uploads из UI продолжают публиковаться в `attachments/`; новые remote results используют `a2a/<local-operation-id>/`, входящие A2A-вложения — `a2a/<local-batch-id>/`. Назначение выводится из authenticated transport/source и не принимается от модели/внешнего клиента.
- [ ] Обновить A2A admission callers и prompt attachment descriptors: файлы входящего поручения доступны в новом каталоге до первого model turn после проверок, а в модель передаются только имена/тип/размер/путь. Существующие Task scope, incoming aggregate limit и запрет доступа к соседним чатам сохраняются.
- [ ] Атомарно связать terminal result, артефакты и batch с canonical task/run/chat. Сохранить current source lineage, cancel/deadline recheck и single-rename recovery.
- [ ] Провести содержимое всех созданных файлов и manifest через whole-batch material review до publication. Частичное разрешение не открывает остальную часть результата.
- [ ] Проверить отказ, detector outage/timeout, публикационный сбой, потерю claim и restart после rename до commit. Проверить отсутствие повторного Send и новых копий файлов.
- [ ] Обновить cleanup и integrity callers для нового каталога; повторное чтение не восстанавливает удалённый файл скрыто.

Проверка: `uv run python -m unittest tests.test_remote_inbound_files tests.test_remote_file_results_runtime tests.test_workspace_files tests.test_workspace_cleanup tests.test_workspace_cleanup_api -v` — exit 0 с PostgreSQL.

### Этап 6. File-only model projection

- [ ] Расширить existing runtime/material tests уникальными canary-значениями в текстовом Artifact и JSON. Проверять фактические model messages, а не только отдельный serializer.
- [ ] Обновить `remote_result_projection`, `_remote_result_batches` и всех их callers: полученные артефакты возвращаются как receipts/paths. В terminal `text` остаются только разрешённые публичные Messages, без Artifact content.
- [ ] Проверить прямые `core_task_wait/get/list`, mailbox notifications, nested Python и восстановленные tool outcomes. Ни один из этих путей не загружает Artifact text/data в контекст автоматически.
- [ ] Добавить контрольный сценарий: модель выбирает конкретный `.json`, Python читает его в sandbox; модель получает содержимое только после этого разрешённого вызова.
- [ ] Сохранить guardrails и visibility после чтения. Отклонённый batch не открывается через файл, owner download, другой handle или иной projection.
- [ ] Проверить явный выбор этого файла для исходящей A2A-задачи и отсутствие автоматических исходящих вложений.

Критерий проверки с canary:

```python
assert "artifact-content-canary" not in automatic_model_context
assert "json-value-canary" not in automatic_model_context
assert "/workspace/a2a/" in automatic_model_context
assert "json-value-canary" in explicit_read_tool_result
```

Проверка: targeted remote file/runtime, Python wait, guardrails и peer conversation suites; PASS для in-memory и PostgreSQL.

### Этап 7. Owner UI и безопасная публичная проекция

- [ ] В `tests/test_peer_conversations.py` и `tests/test_enterprise_release_boundaries.py` проверить публичный вопрос/ответ, owner wait marker, private owner conversation, artifact receipts, скачивание и изоляцию.
- [ ] Обновить peer detail/owner history: одна переписка на контрагента содержит уточнения, ответы и карточки артефактов с сохранённым порядком. Polling не создаёт визуальные дубликаты.
- [ ] Отображать текстовые/JSON артефакты как файлы, открываемые существующим предпросмотром. Краткий статус различает «Нужно уточнение», «Ожидает владельца внешнего агента», «Ответ принят», «Отправка не подтверждена».
- [ ] Сохранить текущие resize панели, выбор контрагента, scroll position, модальные детали действий и accessibility. Не возвращать dropdown отдельных A2A-задач.
- [ ] Расширить существующий `tests/owner_ui_files_browser.mjs` контролируемым peer fixture: вопрос → ответ → повторное ожидание → JSON/text Artifact → открытие файла. Проверить режим owner question и отсутствие содержимого в основном tool result.
- [ ] Проверить owner-only API и внешнюю проекцию после restart: внешнему инициатору не доступны вопросы/ответы владельцев, исходные artifact receipts чужой операции или workspace paths нашего чата.

Проверки из каталога `ui`: `npm run typecheck`, `npm run build` — exit 0. Existing native browser gate: `CORE_AGENT_REQUIRE_BROWSER_TESTS=1 uv run python -m unittest tests.test_owner_ui_files_browser -v` с обязательными fixtures из CI — PASS без skip.

### Этап 8. Итоговые проверки, main и два кластера

- [ ] Выполнить применимые release gates существующим способом: `uv run ruff check core_agent tests`; `uv run python -m unittest discover -s tests -v` с реальными PostgreSQL/Keycloak; `uv build --no-sources`; TypeScript/build; spec quality/hash lock; обязательный native Linux/browser gate.
- [ ] Использовать существующую конфигурацию тестов. До запуска проверить все требуемые зависимости одной проверкой; не печатать секреты и не читать shell alias с proxy credentials.
- [ ] Обновить implementation status только по прошедшим автоматическим проверкам. Обновить README и стабильные факты AGENTS для нового reply contract, каталога и projection; plan checklist отмечать по фактическому выполнению.
- [ ] Проверить diff, scope, compatibility, security и recovery. Сохранять проверенные логические slices отдельными reviewable commits; spec входит до или вместе с первым implementation slice.
- [ ] После всех проверок актуализировать локальную `main`. Не затрагивать существующую `.idea/` и пользовательские файлы; не выполнять remote git push без отдельного запроса.
- [ ] Собрать и опубликовать один immutable amd64 image; проверить пользователя `agent` и source/UI hashes. Через существующий deploy runner обновить оба развёртывания: `https://37.44.196.209/ui/` и `https://37.44.197.46/ui/`.
- [ ] Проверить фактический digest, ready replicas, migration outcome, HTTPS readiness и новые UI assets на обоих кластерах. Между production агентами не создавать соединения или задачи автоматически: ручной путь остаётся пользователю.

## 4. Проверяемый результат

1. Внешний агент спрашивает город; наш агент отвечает в ту же Task и получает итог. Если ответа в контексте нет, вопрос получает владелец.
2. Внешний агент ждёт своего владельца: наш агент ожидает, не подтверждает чужой HITL и не читает внутреннюю переписку.
3. Повторный snapshot и restart не повторяют вопрос модели или уже отправленный ответ; исходный deadline сохраняется.
4. JSON/text/mixed Artifact, самостоятельный A2A FilePart и вложение входящего A2A поручения создают проверенные файлы под `a2a/` workspace своего чата. Manifest сохраняет происхождение и структуру; другой чат не может их прочитать или скачать. UI upload остаётся в `attachments/`.
5. До явного чтения файла модель видит только receipts и пути, а содержимое не попадает в контекст через иной task API или recovery.
6. Artifact открывается в UI как файл; новый polling не дублирует его. Неуспешный/rejected/unpublished batch недоступен.
7. Оба развёртывания используют один проверенный образ, а старые сохранённые задачи продолжают читаться согласно зафиксированной compatibility.

## 5. Согласование и начало работы

Согласуется поведение из раздела 1 и последовательность этапов. После явного согласования начать с нормативного контракта и первых regression cases; текущий документ сам по себе не заявляет runtime capabilities реализованными.
