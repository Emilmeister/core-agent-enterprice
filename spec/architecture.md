# Архитектура целевого продукта

## Архитектурная цель

Core Agent — stateful orchestration kernel с портами для моделей, execution environments, MCP, skills, persistence, policy и событий. Бизнес-логика agent loop не должна зависеть от HTTP framework, конкретного provider или операционной системы.

## Подсистемы

```text
Client / peer agent
        |
     A2A server ---- Control plane
        |                 |
   Run orchestrator -- Policy engine
     /   |   |   \
 Model Context Tool  Skill/MCP managers
 router engine runtime      |
     \   |   |             /
       Durable tasks + event log + artifacts + Markdown memory
```

### A2A server и adapters

A2A является основным внешним контрактом: Agent Card, Messages, Tasks, Artifacts, streaming, polling и push notifications. Library/CLI adapters MAY существовать, но сохраняют A2A semantics и не создают параллельную state machine. Transport не содержит agent logic.

### Run orchestrator

Владеет task state machine, turn loop, budgets, checkpoints, cancellation, background work, durable mailbox, delegation и terminal outcome. Только orchestrator может переводить task/run между состояниями.

### Model router

Предоставляет унифицированный capability-based интерфейс к providers: context window, tool calling, reasoning, streaming, structured output, multimodality, token accounting и idempotency. Выбирает primary/fallback по host policy, данным и budget.

### Context engine

Собирает активный model context из kernel instructions, agent profile, prompt, session state, skills, tool catalog, hybrid memory retrieval и transcript. Владеет рабочим token budget, compaction, retrieval и provenance.

### Tool runtime

Регистрирует built-ins и MCP tools, валидирует calls, передаёт их policy engine, исполняет только через изолированный ExecutionEnvironment, нормализует outputs и фиксирует side effects.

### Skill manager

Разрешает версии, проверяет integrity/signature, строит discovery catalog и лениво загружает инструкции/resources.

### Policy engine

Принимает нормализованный proposed action и возвращает `allow`, `deny` или `require_approval` с причиной и допустимым scope grant. Его решение нельзя переопределить model output-ом.

### Durable state

Event log является источником истины для состояния A2A Task/run. Checkpoints ускоряют восстановление, но MUST быть воспроизводимы или сверяемы с log. Transcript, Markdown memory, derived indexes и artifacts являются отдельными stores с независимыми retention policies. Markdown является source of truth памяти; graph/BM25/vector indexes всегда перестраиваемы.

## Идентификаторы и иерархия

```text
tenant
└── session
    ├── A2A task / run
    │   ├── turn
    │   ├── tool_call
    │   ├── approval
    │   └── artifact
    └── child A2A task / subagent run
```

Все IDs непрозрачны, уникальны и не несут секретной информации. Child Task хранит `parent_task_id`, наследует tenant/session security context и имеет отдельный budget slice.

## State machine

```text
CREATED -> VALIDATING -> QUEUED -> RUNNING
                                |-> WAITING_INPUT
                                |-> WAITING_APPROVAL
                                |-> WAITING_AUTH
                                |-> WAITING_TASK
                                |-> PAUSED
                                |-> CHECKPOINTING -> RECOVERING -> RUNNING
                                `-> COMPLETED | FAILED | CANCELLED | REJECTED | ABORTED
```

- `WAITING_*` и `PAUSED` являются durable: worker может освободить ресурсы.
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
- после compaction и memory commit;
- перед terminal event.

При recovery orchestrator сверяет intent, idempotency key и recorded outcome. Он MAY повторить только доказуемо идемпотентную операцию. Иначе run переходит в `WAITING_INPUT` или `ABORTED` с `SIDE_EFFECT_UNKNOWN`.

## Расширяемость

Стабильные внутренние ports:

- `ModelProvider`;
- `ExecutionEnvironment`;
- `McpTransport`;
- `SkillResolver`;
- `PolicyEvaluator`;
- `SecretResolver`;
- `EventStore`, `ArtifactStore`, `MemoryStore`;
- `SearchIndex`, `GraphIndex`, `EntityExtractor`, `Reranker`;
- `TaskScheduler`, `TaskMailbox`;
- `TelemetryProvider`;
- `EventPublisher`;
- `Tokenizer`.

Adapters объявляют capabilities. Orchestrator MUST проверять их при инициализации, а не падать в середине запуска из-за отсутствующей обязательной функции.

## Concurrency

Последовательное выполнение является семантической базой. Runtime MAY параллелить model/tool/subrun операции, только если:

- зависимости представлены явно;
- targets не пересекаются либо executor обеспечивает isolation;
- approval для каждого side effect независим;
- порядок слияния результатов детерминирован и попадает в audit;
- отмена одной ветви не оставляет другие без владельца.

При сомнении runtime выполняет шаги последовательно.

## Multi-agent delegation

Primary agent создаёт сабагента как неблокирующую A2A Task. Сабагент получает ту же kernel policy и общую memory, но только явно перечисленные parent-ом рабочие tools, MCP capabilities и skills. Полный contract описан в [Фоновых задачах и делегировании](tasks-and-delegation.md).

## Dependency direction

Domain types и state machine не импортируют provider SDK, transport frameworks или platform shell code. Инфраструктурные adapters зависят от core ports, но не наоборот. Это правило MUST проверяться архитектурными tests или package boundaries.
