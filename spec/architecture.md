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

Enterprise меняет authentication, caller ownership, file placement, tool availability и remote wait semantics, сохраняя A2A 1.0 и однополевой RunRequest. Это явная версия application/persistence contract; старые неаутентифицированные endpoints MUST NOT сохраняться как обход новых owner/external границ. Agent Card рекламирует только фактически подключённые bindings/capabilities. Rollout требует обновлённых клиентов/credentials и отдельного migration job с DDL role; serving процесс только проверяет schema version и fail closed при несовпадении.

Migration заранее делает recoverable backup metadata и blobs, проверяет versions/integrity и задаёт явное сопоставление legacy tenant/user/context со стабильной authenticated identity. Нельзя угадывать caller по текущему токену, `anonymous`, имени файла, последнему запросу или общему tenant. Неоднозначные legacy rows/blobs сохраняются изолированно, недоступны внешним caller-ам до подтверждённого operator mapping. Owner-wide UI доступ не расширяет execution scope. Старые пользовательские blobs сохраняются при удалении artifact tools; mapping/move публикуется только после полного успешного переноса и сверки digest.

Wait, cron и file schemas/checkpoints версионируются; migration сохраняет абсолютные deadlines, admission IDs, closed outcomes, visibility и side-effect intent. Старый checkpoint с неизвестным исходом dispatched call переводится в reconciliation, а не переотправляется. Неизвестная schema version не исполняется. Legacy ephemeral workspace нельзя объявить постоянным без переноса на `CHAT_WORKSPACE_ROOT`; старые snapshots не восстанавливают удалённые файлы.

Legacy `REMOTE_AGENTS` и dedicated auth settings импортируются однократно явным migration в owner registry с проверкой target, identity и защищённым хранением header values. Неоднозначный per-agent auth требует operator mapping; входящий credential автоматически не переносится и не проксируется. После cutover UI registry authoritative: рестарт и старое ENV не перезаписывают его, изменения аудируются. Старые values сохраняются защищённо только для согласованного rollback, не в model/transcript/logs.

До первой enterprise mutation допускается rollback на проверенный pre-migration snapshot. После новых waits, owner decisions, cron admissions, file revisions или UI registry edits старый runtime не может обслуживать новые records: сначала drain/quiesce, затем проверенный reverse migration либо восстановление согласованного DB+blob backup с учётом новых данных. Нельзя молча потерять принятые сообщения/файлы, воскресить timer/approval или повторить возможный side effect. Rollback, который не сохраняет эти гарантии, блокируется; простой запуск старого image на новой schema запрещён.
