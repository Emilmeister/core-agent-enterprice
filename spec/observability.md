# OpenTelemetry observability

## Нормативная база

Core Agent MUST быть нативно инструментирован OpenTelemetry для traces, metrics и logs и экспортировать их через OTLP. Реализация следует version-pinned [OpenTelemetry specification](https://opentelemetry.io/docs/specs/otel/) и [Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/).

Текущие GenAI conventions развиваются отдельно от базовых conventions. Реализация MUST фиксировать используемую semantic-convention version; стандартные attributes имеют приоритет, а отсутствующие Core Agent concepts используют namespace `core_agent.*` до появления совместимого стандарта.

Telemetry не заменяет durable audit: sampling или недоступность collector-а не может уничтожить task state/provenance.

## Signals

### Traces

Показывают causal path A2A request, Task, model turns, tools, MCP, memory pipeline, background jobs, сабагентов, approvals и terminal sessions/processes.

### Metrics

Показывают bounded-cardinality latency, throughput, usage, saturation, errors и quality counters.

### Logs

Structured logs описывают operator diagnostics и correlation с trace/span. Prompt, memory content, tool arguments/output и secrets не логируются по умолчанию.

## Context propagation

Core Agent MUST использовать W3C Trace Context через OTel propagators для A2A bindings, MCP HTTP и task queue. Локальные background/subagent процессы получают trace context только через runtime, но trace context не становится authorization.

- `traceparent`/`tracestate` валидируются при extract.
- Входной trace context не даёт authorization или tenant identity.
- Baggage использует allowlist; prompt, user ID, memory text, secrets, paths и tool arguments запрещены.
- Перед внешним недоверенным MCP/A2A endpoint baggage удаляется по default-deny policy.
- Invalid remote context не ломает task и создаёт новый local trace с diagnostic counter.

## Async и background traces

Incoming A2A request span завершается после ответа transport-а и не остаётся открытым на часы. Background Task создаёт новый execution trace со `Span Link` на submission span и attributes A2A task/context IDs.

Сабагент, indexing job и notification delivery используют тот же принцип:

- synchronous child operation MAY быть child span;
- independently scheduled/durable operation получает новый trace + link;
- retry создаёт новый attempt span, связанный с logical task/tool call;
- polling/subscription span не становится parent всей Task;
- push notification delivery имеет отдельный producer/client span и link на task execution.

## Обязательные spans

Минимальная span topology:

```text
core_agent.a2a.message.send / core_agent.a2a.message.stream
└── core_agent.task.submit

core_agent.task.execute  (linked to submit)
├── core_agent.context.assemble
│   └── mcp.client memory.search
├── gen_ai model operation
├── core_agent.policy.evaluate
├── core_agent.tool.execute
│   ├── core_agent.terminal.session
│   ├── core_agent.terminal.process
│   └── mcp operation
└── core_agent.task.checkpoint

memory_service.mcp.request  (remote child via W3C Trace Context)
├── memory_service.search.bm25
├── memory_service.search.vector
├── memory_service.search.graph
├── memory_service.search.rerank
└── memory_service.commit
    ├── memory_service.chunk
    ├── memory_service.embed
    ├── memory_service.ner
    ├── memory_service.entity_resolve
    └── memory_service.index_publish

core_agent.subagent.execute  (linked parent/child A2A Tasks)
core_agent.notification.deliver
```

Core Agent MUST NOT создавать fake internal memory spans: он создаёт MCP client span. Memory Service владеет detailed indexing/retrieval spans и продолжает trace через propagated context.

Операция с duration получает span. Point-in-time transition (`approval required`, `task state changed`, `memory revision published`, `compaction completed`) записывается OTel event/log record с timestamp и безопасными attributes.

## Span attributes

Допустимые high-cardinality IDs на spans/logs, но не metrics: `a2a.task.id`, `a2a.context.id`, `core_agent.run.id`, tool call ID, memory revision, artifact ID и terminal session ID.

Обязательные bounded attributes по применимости:

- service/core version, deployment environment и component;
- operation name, outcome/error type и retry attempt;
- model provider/model capability route и token usage;
- tool namespace/type и risk decision без arguments;
- task type/state, parent/child depth и detached flag;
- memory scope/kind, index revision, retriever type и candidate count;
- compaction base/working tokens и before/after working occupancy;
- terminal owner kind/process state/exit status и cleanup outcome;
- A2A/MCP protocol and extension versions.

Content attributes из GenAI conventions, tool definitions/arguments/results, system instructions, retrieved documents и memory text MUST быть выключены по умолчанию. Их opt-in требует explicit data policy, redaction, sampling и retention limits.

## Metrics

Core Agent MUST публиковать минимум:

- A2A request/task count и latency по operation/state/error class;
- active/queued/waiting/background Tasks и queue age;
- model calls, latency, first-token latency, token usage и estimated cost;
- tool/MCP calls, latency, retries, denials и unknown side effects;
- approvals/input requested, approved, denied и timed out;
- compaction count, base tokens, working before/after ratio и failures;
- Memory MCP client latency/outcome и configured/filtered state;
- background/subagent count, depth, fan-out, duration и budget usage;
- terminal session/process create/reuse/cleanup latency, resource saturation и policy denials;
- checkpoint/recovery/lease/notification delivery outcomes;
- OTLP export drops/failures и telemetry queue saturation.

Memory Service отдельно MUST публиковать search candidate counts/channels/rerank latency, write validation, 200-line rejections, indexing/NER/entity-resolution latency, revision publication и backlog.

Metric labels MUST иметь bounded cardinality. Task/run/user/tenant IDs, prompt, path, command, entity text, memory ID и raw error message запрещены как labels.

## Logs

- LogRecord содержит timestamp, severity, service/resource, trace ID, span ID, component, safe event name и error code.
- Structured error сохраняет exception type/stack только в защищённом operator channel.
- Debug mode не отключает redaction.
- Notification/message payload, raw stdout/stderr и Markdown content не логируются автоматически.
- Tenant data classification определяет exporter, region, retention и access.

## A2A updates и audit

Пользовательская observability идёт через A2A Task status, Messages и Artifact updates. OTel предназначен оператору и не является публичным progress protocol.

Durable audit хранит:

- A2A protocol/extension versions, Message/Task/Artifact revisions;
- kernel/profile/policy versions;
- model routes без hidden reasoning;
- tool intents, safe normalized arguments digests, approvals и outcomes;
- background/subagent contracts и notifications;
- Memory MCP request/result IDs, server/index revisions и safe mutation outcomes; полный candidate/mutation audit принадлежит Memory Service;
- NER/embedding/reranker versions и index publication;
- compaction mappings, checkpoints, recovery и side-effect reconciliation.

Audit record SHOULD хранить trace/span IDs для перехода от product event к telemetry, но остаётся полным при unsampled trace.

## Sampling и failure behavior

- Head/tail sampling policy задаётся deployment-ом и сохраняет errors, policy denials, high latency и recovery traces в пределах privacy policy.
- Telemetry exporter работает асинхронно с bounded queue и не блокирует agent loop.
- Переполнение telemetry queue создаёт metric/log, но не раскрывает dropped payload.
- Collector outage не меняет Task outcome, кроме deployment-а с обязательным compliance audit; тогда блокируется только действие, для которого audit доказуемо обязателен.

## Evals и quality signals

Versioned eval hooks измеряют task completion, compaction fidelity, hybrid retrieval/rerank quality, NER/entity resolution, tool correctness, false allow/deny, лишние approvals, subagent focus, duplicate side effects и cost/latency.

Eval data подчиняется тем же ACL/retention. Evaluation output не становится memory без отдельного validated memory write.
