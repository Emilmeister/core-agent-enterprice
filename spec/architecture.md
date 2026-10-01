# Архитектура целевого продукта

## Архитектурная цель

Core Agent — stateful orchestration kernel с портами для моделей, local terminal sessions, MCP, skills, persistence, policy и событий. Целевой deployment работает в одном Kubernetes Pod, с постоянными папками чатов и обязательным Bubblewrap для команд; бизнес-логика agent loop не зависит от HTTP framework или model provider.

## Подсистемы

```text
Remote caller
      |                             |
 A2A server                  Private control plane
      |                             |
      +------ Run orchestrator -----+
                    |
              Policy/execution gate
     /   |   |   \
 Model Context Tool  Skill/MCP managers   Memory subsystem
 router engine runtime      |
     \   |   |             /
       Durable tasks + event log + artifacts
```

### A2A server и adapters

A2A является основным внешним контрактом: Agent Card, Messages, Tasks, Artifacts, streaming, polling и push notifications. Library/CLI adapters MAY существовать, но сохраняют A2A semantics и не создают параллельную state machine. Transport не содержит agent logic.

### Run orchestrator

Владеет task state machine, turn loop, budgets, checkpoints, cancellation, background work, durable mailbox/inbound inbox, delegation и terminal outcome. Только orchestrator может переводить task/run между состояниями.

### Config compiler

До сетевого discovery собирает и сохраняет immutable admission ceiling как
пересечение PlatformConfig, tenant policy, AgentConfig, Task capabilities и MCP
declarations. После discovery фиксирует EffectiveConfig и полный проверенный MCP
catalog. Новая platform deny может только сузить принятый ceiling; изменение
deployment configuration не расширяет capabilities уже созданной Task. Compiler
удаляет disabled tools/MCP/skills до model discovery и генерирует соответствующую
Agent Card.

### Model router

Предоставляет унифицированный capability-based интерфейс к providers: context window, tool calling, reasoning, streaming, structured output, multimodality, token accounting и idempotency. Выбирает primary/fallback по host policy, данным и budget.

### Context engine

Собирает активный model context из kernel instructions, agent profile, prompt, session state, skills, effective tool catalog, MCP retrieval results и transcript. Владеет рабочим token budget, compaction и provenance. Сам не реализует memory retrieval.

### Tool runtime

Регистрирует built-ins и MCP tools, валидирует calls и передаёт их policy engine. Terminal, skill scripts и stdio MCP запускаются через owned TerminalSession с отдельными PTY/process group в workspace своего чата либо optional isolated scratch-копии; runtime нормализует outputs и фиксирует side effects.

### Skill manager

Разрешает версии, проверяет integrity/signature, строит discovery catalog и лениво загружает инструкции/resources.

### Policy

Policy engine проверяет immutable platform/tenant/mode ceiling и текущую owner policy `allow|require_hitl|deny`. HITL durable связывает точный call/arguments с решением владельца; schema, scope и current policy проверяются перед dispatch. Guardrails независимо допускает конкретную версию материала и не выдаёт capability. UI и owner endpoints находятся за Keycloak, внешний A2A имеет отдельную границу доступа.

### Durable state

Event log является источником истины для состояния A2A Task/run. Inbound Messages durable сохраняются до model delivery и дедуплицируются по `(task_id, message_id)`; inbox append и terminal transition сериализуются без потери подтверждённого input. Checkpoints ускоряют восстановление, но MUST быть воспроизводимы или сверяемы с log. История чата сохраняется без автоматического удаления по возрасту; служебные blobs/checkpoints имеют отдельные retention policies, не удаляющие историю. Память является подсистемой Core Agent и ведёт собственные revisions вне event log; Core сохраняет только использованные tool results/provenance согласно Task retention.

Production adapter хранит A2A Tasks, event log, checkpoints, и append-only audit в PostgreSQL через один bounded pool. `DATABASE_URL` обязателен и берётся из deployment secret. Нет автоматического fallback на process memory/SQLite при database outage: startup/readiness fail closed, активные protected actions не исполняются. Test adapters не могут быть выбраны production configuration.

## Идентификаторы и иерархия

```text
tenant
└── session
    ├── A2A task / run
    │   ├── turn
    │   ├── tool_call
    │   └── artifact
    └── child A2A task / subagent run
```

Все IDs непрозрачны, уникальны и не несут секретной информации. Child Task хранит `parent_task_id`, наследует tenant/session security context и имеет отдельный budget slice.

## State machine

```text
CREATED -> VALIDATING -> QUEUED -> RUNNING
                                |-> WAITING_INPUT
                                |-> WAITING_AUTH
                                |-> WAITING_TASK
                                |-> PAUSED
                                |-> CHECKPOINTING -> RECOVERING -> RUNNING
                                `-> COMPLETED | FAILED | CANCELLED | REJECTED | ABORTED
```

- `WAITING_*`, `APPROVED_RESERVED` и `PAUSED` являются durable: worker может освободить ресурсы.
- `WAITING_TASK` означает пассивное ожидание background task без busy polling и без занятого model worker.

Durable wait хранится в schema 14 в `core_waits`: immutable scope run/tenant/owner/context,
монотонная generation, kind, source ID, subject, versioned continuation, абсолютный
deadline и отдельные resolved/applied timestamps. Единственное ещё не applied
ожидание run создаётся атомарно с checkpoint и освобождением lease. Outcome выбирается
однократно под run→wait locks; время дедлайна берётся после lock. Applied отмечается
только транзакцией, которая включила результат в checkpoint. Закрытие terminal run
также закрывает его wait. Private subject не публикуется в A2A status/outbox.

Runtime возвращает отдельный `SuspendedRun`, который transport и scheduler MUST
распознавать до сериализации: он не является финальным ответом, Artifact или ошибкой.
Неразрешённое ожидание не выбирается для execution recovery. Решение или timeout
делает тот же run готовым к fenced recovery; хранение нового результата не даёт
само по себе execution lease.
- `ABORTED` означает, что continuation невозможно доказать безопасным.
- Переход записывается в event log до публикации соответствующего события.
- Ровно один actor владеет lease на изменение run; истёкший lease не даёт права повторять внешний side effect.

## Durable execution

Checkpoint MUST создаваться:

- после валидации и snapshot расширений;
- перед внешним mutating call;
- после фиксации его outcome;
- перед ожиданием человека;
- после compaction и подтверждённого mutating MCP outcome;
- перед terminal event.

При recovery orchestrator сверяет intent, idempotency key и recorded outcome. Он MAY повторить только доказуемо идемпотентную операцию. Иначе run переходит в `WAITING_INPUT` или `ABORTED` с `SIDE_EFFECT_UNKNOWN`.

## Расширяемость

Стабильные внутренние ports:

- `ModelProvider`;
- `TerminalSessionManager`;
- `McpTransport`;
- `SkillResolver`;
- `PolicyEvaluator`;
- `SecretResolver`;
- `EventStore`, `ArtifactStore`;
- `TaskScheduler`, `TaskMailbox`;
- `TelemetryProvider`;
- `EventPublisher`;
- `Tokenizer`.

Adapters объявляют capabilities. Orchestrator MUST проверять их при инициализации, а не падать в середине запуска из-за отсутствующей обязательной функции.

## Concurrency

Последовательное выполнение является семантической базой. Runtime MAY параллелить model/tool/subrun операции, только если:

- зависимости представлены явно;
- targets не пересекаются либо agents используют отдельные local workspace copies;
- порядок слияния результатов детерминирован и попадает в audit;
- отмена одной ветви не оставляет другие без владельца.

При сомнении runtime выполняет шаги последовательно.

## Multi-agent delegation

Primary agent создаёт сабагента как неблокирующую A2A Task. Сабагент получает ту же kernel policy, но только явно перечисленные parent-ом рабочие tools, MCP capabilities и skills. Общая memory существует лишь когда delegation явно передаёт `core_memory_*` tools: child наследует ту же scope-тройку. Полный contract описан в [Фоновых задачах и делегировании](tasks-and-delegation.md).

## Dependency direction

Domain types и state machine не импортируют provider SDK, transport frameworks или platform shell code. Инфраструктурные adapters зависят от core ports, но не наоборот. Это правило MUST проверяться архитектурными tests или package boundaries.


## Enterprise admission и долговечные состояния

Одна компания имеет владельцев и внешних caller-ов со стабильным authenticated scope. Чат привязан к tenant и доказанному caller scope; owners видят все чаты компании, но выполняющаяся Task получает только scope своего чата. Admission атомарно проверяет один active root на чат, сохраняет `(tenant, stable caller, messageId)` и digest content/context/attachments, Task/inbox и files publication. Duplicate сначала возвращает прежнюю Task; changed content конфликтует. Неуспешный CONTEXT_BUSY является отдельной durable terminal Task, не занимает чат и не создаёт очередь.

Waits, решения, timer generation, remote handle↔IDs, final timeout, cron revision/ticks, file staging/publication/quarantine и workspace revision имеют versioned structured records в существующей PostgreSQL workflow модели. Summary не является их источником истины. Lease/fencing и atomic transitions допускают одно продолжение; новые retries не воспроизводят неизвестный side effect. Ожидание освобождает execution worker, сохраняя занятость чата. Cleanup и новый root/cron admission сериализуются по тому же chat guard.
Canonical chat writers используют совместимый с FK KEY SHARE row lock
`FOR NO KEY UPDATE`; chat tenant/context/owner keys неизменяемы и защищены БД.
Последний root/revision можно менять, не нарушая взаимное исключение writers.
Cron schedules и append-only events имеют storage version1; schedule не является
второй execution state machine. Automatic admission/event/next due коммитятся
одной транзакцией на connection, владеющей company leader session lock.
Owner CRUD использует обычный pool; dedicated leader session допускает pool
размера1. Memory backend получает все asyncio locks до общего синхронного
workflow/Task/schedule commit; threading RLock никогда не удерживается через
await. Recovery thread только планирует один bounded memory scan на ASGI loop.
Cron не запускает model/tool до commit и не выдаёт operator authority.


## Совместимость, migration и rollback

### Schema 13: admission корневой задачи

`core_chats` хранит `(tenant_id, context_id)` как primary key, immutable
`owner_id`, nullable `latest_root_run_id` со ссылкой на `core_runs`,
`schema_version=1` и время создания. Последний root — историческая ссылка:
занятость определяется его canonical workflow state, а не вторым lifecycle.
Все нетерминальные states, включая ожидания, занимают чат. Terminal state
освобождает слот без удаления истории и без изменения указателя старым worker.
Следующий принятый root атомарно заменяет указатель.

`core_root_messages` хранит primary key `(tenant_id, actor_id, message_id)`,
`request_digest`, `fingerprint_version=1`, `owner_id`, `context_id`, `task_id`,
`schema_version=1` и время создания. `actor_id` — verified caller, отдельно от
общего execution owner владельцев. Ledger не удаляется автоматическим TTL.
Прежний run-family retention не применяется к runs зарегистрированного
enterprise чата: возвращается `RETENTION_PROHIBITED` до изменений. Очистка
выбранных workspace файлов не удаляет Task, историю или creation ledger.
Начальный Message сохраняется в A2A Task.history в той же транзакции, поэтому
crash до первого SDK события не теряет запрос. Повторное добавление этого
Message SDK не создаёт копию в истории.

Admission берёт transaction-scoped lock по dedup key, проверяет повтор, затем
создаёт/блокирует chat row и проверяет scope. Task, ledger и, при успешном старте,
workflow/checkpoint/audit/outbox, initial lease и budget reservation коммитятся
вместе. Busy-отказ сохраняет только свою failed Task и ledger, без workflow,
worker или budget reservation. Model/MCP/tool execution допускается после commit.
Чтение state последнего root под chat lock не требует встречного run lock;
terminal transition сохраняет существующие fencing и atomicity.

Migration 12→13 добавляет таблицы, indexes и application grants, сохраняя
legacy rows и blobs. Старые context IDs без явного migration mapping не
присваиваются новым caller-ам. До применения DDL останавливаются старые
admission workers; отдельная migration job обновляет schema, затем стартует
runtime schema 13. После новых chat/ledger writes старый image не может
обслуживать базу без согласованного reverse migration/backup restore.

Migration 13→14 добавляет durable waits, не меняя существующие deadlines или
side-effect markers. Перед DDL старые workers останавливаются; migration job
создаёт таблицу, constraints и grants, затем запускается image со schema 14.
После появления wait records rollback требует согласованного DB backup/restore,
а не запуска schema-13 runtime поверх новых записей.

Enterprise меняет authentication, caller ownership, file placement, tool availability и remote wait semantics, сохраняя A2A 1.0 и однополевой RunRequest. Это явная версия application/persistence contract; старые неаутентифицированные endpoints MUST NOT сохраняться как обход новых owner/external границ. Agent Card рекламирует только фактически подключённые bindings/capabilities. Rollout требует обновлённых клиентов/credentials и отдельного migration job с DDL role; serving процесс только проверяет schema version и fail closed при несовпадении.

Migration заранее делает recoverable backup metadata и blobs, проверяет versions/integrity и задаёт явное сопоставление legacy tenant/user/context со стабильной authenticated identity. Нельзя угадывать caller по текущему токену, `anonymous`, имени файла, последнему запросу или общему tenant. Неоднозначные legacy rows/blobs сохраняются изолированно, недоступны внешним caller-ам до подтверждённого operator mapping. Owner-wide UI доступ не расширяет execution scope. Старые пользовательские blobs сохраняются при удалении artifact tools; mapping/move публикуется только после полного успешного переноса и сверки digest.

Wait, cron и file schemas/checkpoints версионируются; migration сохраняет абсолютные deadlines, admission IDs, closed outcomes, visibility и side-effect intent. Старый checkpoint с неизвестным исходом dispatched call переводится в reconciliation, а не переотправляется. Неизвестная schema version не исполняется. Legacy ephemeral workspace нельзя объявить постоянным без переноса на `CHAT_WORKSPACE_ROOT`; старые snapshots не восстанавливают удалённые файлы.

Legacy `REMOTE_AGENTS` и dedicated auth settings импортируются однократно явным migration в owner registry с проверкой target, identity и защищённым хранением header values. Неоднозначный per-agent auth требует operator mapping; входящий credential автоматически не переносится и не проксируется. После cutover UI registry authoritative: рестарт и старое ENV не перезаписывают его, изменения аудируются. Старые values сохраняются защищённо только для согласованного rollback, не в model/transcript/logs.

Registry storage version1 хранит company-scoped current peer pointer и append-only
peer revisions; каждая revision содержит безопасную конфигурацию, encrypted
credential, actor и timestamp. Name уникален в company и immutable. DELETE —новая
disabled revision, не физическое удаление. Settings добавляются с defaults в
существующую owner settings row. Schema migration19 additive: прежние local
tasks/workflows остаются читаемы; serving image требует актуальную schema.
Backup/rollback сохраняет старые revisions и encryption key; после записи новых
state formats допустим только image, читающий их. Новые owner registry API сами
по себе не означают, что legacy synchronous transport заменён durable lifecycle.

До первой enterprise mutation допускается rollback на проверенный pre-migration snapshot. После новых waits, owner decisions, cron admissions, file revisions или UI registry edits старый runtime не может обслуживать новые records: сначала drain/quiesce, затем проверенный reverse migration либо восстановление согласованного DB+blob backup с учётом новых данных. Нельзя молча потерять принятые сообщения/файлы, воскресить timer/approval или повторить возможный side effect. Rollback, который не сохраняет эти гарантии, блокируется; простой запуск старого image на новой schema запрещён.


### Schema 15: настройки владельцев

`core_owner_settings` хранит company-scoped revision и три timeout owner API.
`core_tool_policies` хранит revision, mode и guardrails_exempt под составным
ключом `(tenant_id, canonical_name, origin)`. Отсутствующая policy означает
require_hitl/false/revision=0. Оба хранилища меняются через CAS; CHECK constraints
сохраняют допустимые mode и положительные bounded timeout. Migration не
выдаёт существующим инструментам автоматическое разрешение.

Serving process требует совпадения schema version; отдельная migration job
выполняется после остановки старых workers. Переход с schema 14 сохраняет все
wait deadlines/outcomes; решения владельцев продолжают использовать core_waits.
После новых policy/decision writes откат image требует согласованного отката БД.

### Private file batches: schema 16

`core_chat_file_batches` хранит version-1 manifest, trusted tenant/actor/message,
исходный request digest, original created_at, upload lease и server-owned storage
key. До admission context/owner/task/run не считаются закреплёнными. Acceptance
связывает их с существующим chat/workflow в той же транзакции, где принимается
root или follow-up. После принятия manifest и scope неизменяемы; отдельные состояния
`staging`, `accepted_quarantine`, `accepted_ready`, `published`, `excluded`,
`rejected` описывают хранение, не заменяют A2A Task lifecycle.

Все bytes и complete manifest сохраняются с fsync до acceptance. Publication
допускается после committed guardrail decision и одной атомарной операцией без
замены публикует весь каталог в readonly attachments. Проверка live Task и
публикация сериализуются с terminal/cancel через chat/workflow locks. После
rename и до published commit recovery проверяет тот же immutable manifest и
не создаёт вторую копию. Ошибка после acceptance сохраняет pending delivery,
а не имитирует непринятое сообщение.

Rollback admission не удаляет bytes автоматически: вызывающий слой после
выхода из транзакции отклоняет и очищает только свой непринятый stage. При
аварии действует original-age sweeper с проверкой lease и authoritative
references; active uploads и accepted/quarantined данные не удаляются.
Перезапуск не обновляет возраст. Migration 16 сохраняет прежние rows без
вложений, выдаёт serving role только SELECT/INSERT/UPDATE новой таблицы и
не открывает binary intake сама по себе. Rollback требует согласованного
состояния БД и volume по общему контракту выше.

Migration 23 изменяет только guardrail transition trigger, сохраняя shape и
manifest всех file batches. Для `accepted_ready → excluded` допускается новая
ссылка на отказ по точному материалу; прежнее одобрение остаётся в immutable
material review и workflow history. Остальные решения и `published` неизменяемы.
Если rename в workspace завершился до сбоя publication transaction, перед
записью отказа тот же проверенный каталог возвращается в private quarantine.
Неоднозначный или изменённый target не удаляется и требует reconciliation.
Serving image требует schema 23; migration job выполняется при остановленных
старых workers. После superseding decision rollback требует согласованного
DB+volume backup, а не запуска schema-22 image на новых записях.

### Material reviews: schema 17

`core_material_reviews` хранит owner-private immutable payload или sealed file
reference, trusted tenant/run/source ID/source kind, content digest и version 1.
Ключ `(tenant_id, run_id, source_id, content_digest)` обеспечивает повторное
получение той же проверки. Digest относится к полному payload/version; исходный
материал и scope не меняются после создания. Ссылки на review не являются
разрешением прочитать чужой чат и не публикуются в A2A metadata/history.

Состояния `checking`, `clear`, `pending`, `allowed`, `rejected`, `timed_out`
описывают решение о материале. Таблица сохраняет detector deadline, лимиты,
начисленные attempts/input tokens, наблюдаемый usage, revision и ссылку на
существующий `core_waits`. Attempt начисляется до сети под текущей run lease.
После interruption попытка не становится бесплатной и не запускается повторно
автоматически: незавершённая проверка требует owner decision. Repeated source
не переоткрывает уже закрытый отказ после смены exemption или compaction.

Для inline материала runtime сохраняет в private `completed_result_ref` также
канонический material digest, независимый от ID вызова и transport envelope.
Negative lookup использует tenant/owner/context, включая предыдущие runs этого
чата, и authoritative outcome связанного wait. Он выполняется до exemption и
нового classifier call: rejected/timed-out exact payload не становится доступен
через повторный вызов другого tool. Source digest продолжает связывать review
с конкретным continuation/вызовом. Проверяется равенство точного текста или
канонического JSON содержимого; это не сравнение смысла перефразированных либо
закодированных представлений и не оценка качества детектора.

Pending review и guardrail wait создаются атомарно с continuation текущего
workflow. Owner resolution, deadline и cancel используют существующий wait
contract; material outcome сверяется с ним до раскрытия payload. Clear/allow
не отменяет current policy, scope, schema или проверки live Task. Result review
ссылается на сохранённый completed outcome и никогда не разрешает повтор
исходного tool side effect. Serving role не получает удаления reviews или DDL;
rollout/rollback выполняется по общей схеме version-check и согласованного backup.

### Company attachment limit: schema 18

Migration 18 добавляет `core_owner_settings.attachment_limit_bytes` с default
25 000 000 и положительным integer constraint. Текущие rows получают этот default;
существующие review/workflow/file manifest не меняются. Лимит входит в прежний
settings CAS; старый timeout-only update сохраняет его. Serving role продолжает
использовать существующий table grant без DDL. Перед новым image выполняется
migration job; старый image со schema 17 не обслуживает schema 18 и не выполняет
автоматический downgrade. Откат требует согласованного backup БД и volumes.

### Remote operation checkpoint: schema 20

Migration20 добавляет nullable JSONB `checkpoint` в `core_background_tasks`.
Старые local task rows имеют null и сохраняют прежний contract. Remote kind
`remote_a2a` использует version1 contract/checkpoint из LONG-02; unknown version
отклоняется fail-closed. Immutable destination ссылается на сохранённую registry
revision, credentials остаются там. Отдельный remote `core_runs` workflow и
model budget ledger не создаются. Existing task claim/heartbeat/revision и
notifications/outbox являются единственными механизмами ownership и completion.

Checkpoint, terminal outcome и notification/outbox коммитятся атомарно.
Timeout закрывает operation через bounded coordinator pass даже во время
in-flight request; поздняя claim не может изменить исход. Recovery без committed
Send marker допускает первую отправку, marker без remote ID требует reconciliation,
известный ID допускает только GetTask. Один Cancel marker не повторяется после
restart. Serving role не получает DDL; schema upgrade выполняет migration job.
После первой remote checkpoint записи rollback требует image, читающий version1,
либо согласованное восстановление DB и registry key/revisions. Старый image
не может обслуживать эти rows или автоматически выполнить downgrade.

Parent workflow snapshot закрепляет remote call до HITL в `remote_calls`:
ключ включает attempt и call ID, значение version1 содержит exact immutable
operation contract LONG-02. Повторённый моделью call ID в следующем attempt
не переиспользует прежний destination или message ID. Approval subject включает
этот safe contract; ожидание HITL и registry update не меняют уже закреплённую
revision. Credentials и Card body в snapshot/subject отсутствуют.
`remote_admission` version1 связывает source ID, attempt и deterministic local
task ID. Existing scheduler admission и fenced parent snapshot коммитятся одной
транзакцией; recovery возвращает принятый handle без нового Send. Старые snapshots
без этих optional fields сохраняют прежнюю recovery semantics, а unknown version
отклоняется до dispatch. Rollback reader обязан понимать эти fields вместе с
operation version1. Вложенный Python call MAY использовать существующее durable
поднятие call в continuation, не создавая второй mutating admission path.

Root Task progress является проекцией committed remote scheduler rows, а не новым
lifecycle. Existing TaskStore reconciliation сохраняет safe metadata вместе с
существующей push enqueue в той же transaction; serving не запускает новый
outbox dispatcher. Get/List и stale SDK save используют тот же scope/projection,
не стирают актуальное progress и не меняют прежний workflow terminal-reconcile
contract. В memory source читается под workflow→scheduler lock order; PostgreSQL
повторно читает canonical root и scoped rows после root Task lock.
Progress не меняет workflow version, wait outcome, lease или budgets и не будит
агента. Повторный polling того же enum сохраняет прежнюю отображаемую revision
и не создаёт новое status/push событие. Passive subscription видит safe metadata
даже при открытом live queue; первое terminal event сохраняет прежнюю границу.
Terminal root подавляет progress, late operation update его не возвращает.
Projection additive: schema20 не расширяется, старый клиент MAY игнорировать
metadata, а rollback requirements operation/checkpoint остаются прежними.

### Workspace cleanup: schema 21

Migration21 добавляет `core_chats.workspace_revision` и таблицу
`core_workspace_cleanups`, связанную с canonical company/chat/owner. Immutable
intent version1 хранит request ID, actor, исходную упорядоченную selection,
подтверждённые identity/content proofs и base revision. Mutable receipt хранит
только результаты уже подтверждённой операции. Повторное использование request
ID с другой selection отклоняется; completed receipt и immutable поля защищены
от изменения. Serving role получает только необходимые SELECT/INSERT/UPDATE,
а schema upgrade выполняется отдельной migration job.

Intent коммитится до первого файлового side effect. Выполнение и recovery
используют canonical chat lock, затем operation lock; pending/reconciliation
intent блокирует admission новых root Tasks в этом чате. Уже принятая duplicate
Task остаётся доступной. Служебные capture objects и fsynced deletion proofs
живут в private cleanup directory вне model workspace. Один и тот же recovery
coordinator продолжает сохранённое намерение после restart; отдельный публичный
Task lifecycle или очередь неподтверждённых удалений не создаются.

После хотя бы одного подтверждённого удаления workspace revision увеличивается
однократно при завершении операции. Preview cursor/identity version2 закрепляют
эту revision; прежний cursor отклоняется, прежний identity token означает stale
selection. История чата, transcript и бюджеты не меняются. Snapshot recovery не
восстанавливает удалённый пользовательский файл поверх persistent workspace.

Upgrade требует image, читающий schema21 и cleanup intent version1. После первой
записи intent старый image не может обслуживать cleanup rows или обходить их
admission barrier. Откат требует совместимого reader либо согласованного
восстановления БД и volumes, включая private journal; восстановление одного
компонента не считается безопасным rollback и не разрешает повторное слепое
удаление или воскрешение уже удалённых файлов.
