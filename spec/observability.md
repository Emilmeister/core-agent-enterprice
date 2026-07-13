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

Runtime MUST писать в stdout контейнера однострочные JSON records минимум для Task lifecycle, model turn/action, tool call/outcome, approval, background/subagent lifecycle, compaction и terminal errors. Record содержит canonical tool name, run/task/context IDs и trace/span IDs при наличии. `CORE_AGENT_LOG_CONTENT=false` является безопасным default; explicit local operator profile MAY включить bounded prompt, public model response, tool arguments и normalized output через `CORE_AGENT_LOG_CONTENT=true`. Перед записью content проходит redaction и truncation.

Raw chain-of-thought/private reasoning не логируется даже в content mode. Вместо него model record показывает только безопасное состояние решения (`continue_reasoning`, `request_tools`, `final_answer`), finish reason и token usage. Public reasoning summary MAY логироваться только если provider явно вернул её как пользовательский контент, а не hidden scratchpad.

## OTLP deployment configuration

Runtime MUST поддерживать стандартные OTLP/HTTP environment variables: общий base endpoint `OTEL_EXPORTER_OTLP_ENDPOINT` и точные per-signal endpoints `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`, `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`. Per-signal value имеет приоритет над общим endpoint. Если задан общий endpoint, runtime добавляет стандартные paths `/v1/traces`, `/v1/metrics`, `/v1/logs`; per-signal value уже является полным URL и не изменяется.

Production profile MUST предоставить destination для всех трёх signals, напрямую или через OTel Collector. Deployment с backend-ом, принимающим только часть signals, MUST задавать только поддерживаемые per-signal endpoints и не отправлять ему неподдерживаемые requests. Отсутствующий endpoint не отключает instrumentation и не влияет на durable audit.

Local Docker Compose profile MUST запускать version-pinned Arize Phoenix с PostgreSQL persistence в отдельной schema, bounded retention и выключенной Phoenix product telemetry. Core Agent и Memory Service отправляют туда только OTLP traces через `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`; Phoenix UI и HTTP collector доступны на configurable host port, по умолчанию `6006`. Metrics/logs этого development profile могут быть направлены в отдельный collector через соответствующие per-signal variables.

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

## Phoenix/OpenInference presentation contract

Phoenix является operator UI, а не только хранилищем произвольных OTel spans. Для предсказуемого отображения runtime MUST экспортировать совместимые с [OpenInference semantic conventions](https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md) attributes; Phoenix translation других conventions не считается заменой явному контракту.

Каждый AI-related span MUST иметь `openinference.span.kind`. Минимальное отображение:

- `core_agent.task.execute` имеет kind `AGENT`, `agent.name`, `session.id`, A2A task/context IDs, пользовательский input и публичный final output;
- `gen_ai.chat` имеет kind `LLM`, `llm.system`, `llm.model_name`, JSON `llm.invocation_parameters`, `input.value`/`output.value` с MIME type, flattened `llm.input_messages.<n>.message.*` и `llm.output_messages.<n>.message.*`;
- весь фактически доступный модели catalog публикуется как JSON schemas в `llm.tools.<n>.tool.json_schema`; assistant tool calls публикуются в `llm.output_messages.<n>.message.tool_calls.<n>.tool_call.*` с ID, function name и JSON arguments;
- известный model usage публикуется одновременно в OpenInference `llm.token_count.*` и совместимых `gen_ai.usage.*` attributes;
- `core_agent.tool.execute` имеет kind `TOOL`, `tool.name`, description, JSON input/parameters, JSON/text output, outcome и tool-call ID;
- orchestration, policy, checkpoint и protocol spans имеют осмысленный `CHAIN`, а memory retrieval/rerank/embed spans — `RETRIEVER`, `RERANKER` или `EMBEDDING` соответственно;
- успешный span завершается OTel status `OK`, exception — `ERROR` с безопасным exception type без raw error message.

Поля `input.value`, `output.value`, message content, tool schemas/descriptions/arguments/results являются content. Runtime MUST фильтровать их как при создании span, так и при последующем добавлении attributes. Deployment включает их только через `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`. Отсутствующая переменная эквивалентна `false`; local Compose profile явно включает её для ограниченного operator-only Phoenix и документирует retention. Production MUST оставить её выключенной, пока отдельная policy не определит access control, redaction, sampling и retention.

Даже при content capture запрещено экспортировать provider credentials, secret values, raw chain-of-thought/private reasoning, MCP authorization headers и полный RunRequest с transport credentials. System/kernel/profile instructions разрешены только в этом привилегированном operator channel и не становятся A2A output, log или audit content.

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
- Notification/message payload, raw stdout/stderr и Markdown content не логируются автоматически; explicit operator content mode MAY писать их bounded/redacted представление в stdout, но не raw chain-of-thought или credentials.
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
