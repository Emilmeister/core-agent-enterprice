# Архитектура целевого продукта

## Архитектурная цель

Core Agent — stateful orchestration kernel с портами для моделей, execution environments, MCP, skills, persistence, policy и событий. Бизнес-логика agent loop не должна зависеть от HTTP framework, конкретного provider или операционной системы.

## Подсистемы

```text
Client / SDK / CLI
        |
   API adapters ---- Control plane
        |                 |
   Run orchestrator -- Policy engine
     /   |   |   \
 Model Context Tool  Skill/MCP managers
 router engine runtime      |
     \   |   |             /
       Durable state + event log + artifacts + memory
```

### API adapters

Преобразуют library, HTTP, WebSocket, queue или CLI transport в одинаковые RunRequest, RunEvent и control commands. Transport не содержит agent logic.

### Run orchestrator

Владеет state machine, turn loop, budgets, checkpoints, cancellation, delegation и terminal outcome. Только orchestrator может переводить run между состояниями.

### Model router

Предоставляет унифицированный capability-based интерфейс к providers: context window, tool calling, reasoning, streaming, structured output, multimodality, token accounting и idempotency. Выбирает primary/fallback по host policy, данным и budget.

### Context engine

Собирает активный model context из prompt, session state, skills, tool catalog, memory и transcript. Владеет token budget, compaction, retrieval и provenance.

### Tool runtime

Регистрирует built-ins и MCP tools, валидирует calls, передаёт их policy engine, исполняет в sandbox, нормализует outputs и фиксирует side effects.

### Skill manager

Разрешает версии, проверяет integrity/signature, строит discovery catalog и лениво загружает инструкции/resources.

### Policy engine

Принимает нормализованный proposed action и возвращает `allow`, `deny` или `require_approval` с причиной и допустимым scope grant. Его решение нельзя переопределить model output-ом.

### Durable state

Event log является источником истины для состояния run. Checkpoints ускоряют восстановление, но MUST быть воспроизводимы или сверяемы с log. Transcript, memory и artifacts являются отдельными stores с независимыми retention policies.

## Идентификаторы и иерархия

```text
tenant
└── session
    ├── run
    │   ├── turn
    │   ├── tool_call
    │   ├── approval
    │   └── artifact
    └── child run
```

Все IDs непрозрачны, уникальны и не несут секретной информации. Child run хранит `parent_run_id`, наследует tenant/session security context и имеет отдельный budget slice.

## State machine

```text
CREATED -> VALIDATING -> QUEUED -> RUNNING
                                |  |  |  \
                  WAITING_INPUT <-+  |   +-> WAITING_APPROVAL
                  WAITING_TOOL  <--+  +----> PAUSED
                                |
                         CHECKPOINTING
                                |
                            RECOVERING
                                |
              COMPLETED | FAILED | CANCELLED | ABORTED
```

- `WAITING_*` и `PAUSED` являются durable: worker может освободить ресурсы.
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

Primary agent MAY создать child run через внутренний `core.delegate`, если policy разрешает и делегирование уменьшает latency или контекстную нагрузку. Child run:

- получает узкий prompt, выбранные MCP/skills и budget;
- не получает секреты, approvals и memory автоматически;
- не может ослабить parent policy;
- возвращает structured result с provenance;
- не общается с конечным пользователем напрямую;
- имеет ограничение depth/fan-out.

Делегирование является implementation capability, а не требованием к клиенту строить multi-agent topology.

## Dependency direction

Domain types и state machine не импортируют provider SDK, transport frameworks или platform shell code. Инфраструктурные adapters зависят от core ports, но не наоборот. Это правило MUST проверяться архитектурными tests или package boundaries.
