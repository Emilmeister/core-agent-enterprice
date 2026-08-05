# OpenTelemetry observability

## Нормативная база

Core Agent MUST быть нативно инструментирован OpenTelemetry для traces, metrics и logs и экспортировать их через OTLP. Реализация следует version-pinned [OpenTelemetry specification](https://opentelemetry.io/docs/specs/otel/) и [Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/).

Текущие GenAI conventions развиваются отдельно от базовых conventions. Реализация MUST фиксировать используемую semantic-convention version; стандартные attributes имеют приоритет, а отсутствующие Core Agent concepts используют namespace `core_agent.*` до появления совместимого стандарта.

Telemetry не заменяет durable audit: sampling или недоступность collector-а не может уничтожить task state/provenance.

## Signals

### Traces

Показывают causal path A2A request, Task, model turns, tools, MCP, memory pipeline, background jobs, сабагентов и terminal sessions/processes.

### Metrics

Показывают bounded-cardinality latency, throughput, usage, saturation, errors и quality counters.

### Logs

Structured logs описывают operator diagnostics и correlation с trace/span. Prompt, memory content, tool arguments/output и secrets не логируются по умолчанию.

Runtime MUST писать в stdout контейнера однострочные JSON records минимум для Task lifecycle, model turn/action, tool call/outcome, background/subagent lifecycle, compaction и terminal errors. Record содержит canonical tool name, run/task/context IDs и trace/span IDs при наличии. `CORE_AGENT_LOG_CONTENT=false` является безопасным default; explicit local operator profile MAY включить bounded prompt, public model response, tool arguments и normalized output через `CORE_AGENT_LOG_CONTENT=true`. Перед записью content проходит redaction и truncation.

Без content mode model record показывает только безопасное состояние решения (`continue_reasoning`, `request_tools`, `final_answer`), наличие reasoning, finish reason и token usage. При `CORE_AGENT_LOG_CONTENT=true` provider-returned visible reasoning/summary MAY записываться отдельным полем `reasoning` после redaction и truncation. Provider-hidden chain-of-thought, encrypted/redacted thinking, signatures и opaque replay data не логируются никогда.

## Диагностика конфигурации при старте

Runtime MUST записать при старте одну структурированную запись `startup.configuration`, описывающую фактически собранную конфигурацию. Она отвечает на вопрос «почему capability отсутствует» без чтения кода и без повторного развёртывания.

Запись MUST содержать как минимум:

- runtime mode и итоговый список built-in tools в каталоге;
- объявленные MCP-серверы и allowlist их тулов;
- заданные в конфигурации URL удалённых агентов, имена подключившихся и причины отказа для остальных. Одного списка подключившихся недостаточно: пустой список одинаково выглядит и когда переменная не дошла до контейнера, и когда она дошла, но ни один пир не ответил, а действия оператора в этих случаях противоположны. URL выводится без userinfo.
- backend артефактов, session/task storage и streaming;
- фактический OTLP endpoint по каждому сигналу и булев признак наличия credentials, но не их значение;
- имя модели, её API format и endpoint host.

Дополнительно runtime MUST записать `capabilities.resolved` при первом разрешении capabilities в процессе: она содержит фактический каталог tools модели и, отдельно по серверам, обнаруженные и разрешённые MCP-тулы. Startup-записи недостаточно, потому что MCP-каталог становится известен только при подключении, а расхождение между обнаруженным и разрешённым и есть типичная причина «тул не виден».

Экспорт телеметрии отказывает кодом транспорта, который SDK печатает без адреса. Поэтому конфигурация экспортёра MUST присутствовать в `startup.configuration` отдельно по сигналам: `403` на логах при работающих трассах означает либо другой endpoint, либо недостаточную область ключа, и без адреса эти случаи неразличимы.

Обе записи MUST подчиняться общим правилам редактирования: секреты, полные URL с credentials и содержимое не выводятся. Значение credential MUST NOT попадать в запись; допустимо имя переменной и host.

Записи MUST выводиться независимо от `CORE_AGENT_LOG_CONTENT`: это описание конфигурации, а не содержимого.

Помимо структурированной записи runtime MUST напечатать при старте одну короткую однострочную запись обычным текстом о состоянии `REMOTE_AGENTS`: заданные URL без userinfo и имена подключившихся, а при отсутствии значения — предупреждение о том, что `core_agent_send_message` недоступен, с указанием, переменная не задана вовсе или задана пустой. Эти два случая требуют противоположных действий: в первом переменной нет в развёртывании, во втором платформа не подставила значение, — а трактовка пустого значения как незаданного их уравнивает. В том же предупреждении runtime MUST перечислить имена присутствующих переменных окружения, относящихся к агентам, без значений: платформа развёртывания может публиковать список пиров под собственным именем, и без перечня имён оператор не отличит «платформа ничего не передала» от «передала под другим именем». Значения не выводятся, потому что переменная может оказаться credential. `startup.configuration` — самая длинная строка, которую пишет процесс, и сборщики логов развёртывания усекают или отбрасывают её именно тогда, когда конфигурация сложна; короткая строка сохраняет тот единственный факт, который отличает «переменная не доехала» от «пиры отказали». Требование к длинной записи это не отменяет.

Runtime MUST напечатать при старте инвентарь переменных окружения — только имена и состояние, никогда значения — двумя группами:

- переменные, к которым обратился этот старт, с состоянием `set`, `empty` или `missing`;
- переменные, присутствующие в контейнере, к которым старт не обращался, с тем же состоянием.

Вторая группа отвечает на вопрос, на который первая ответить не может: платформа развёртывания публикует нужное значение под собственным именем, и без перечня присутствующих имён это неотличимо от отсутствия значения вовсе. Именно так обнаруживается пара вроде `URL_AGENT` при читаемом `AGENT_URL`.

Разделение описывает обращение, а не наличие значения, поэтому состояние выводится для обеих групп: переменная, к которой обращается только один путь кода, попадёт во вторую группу на другом пути, и оператору всё равно нужно видеть, задана она или пуста.

Инвентарь MUST разбиваться на короткие нумерованные строки вида `i/N`. Одна длинная строка здесь не годится по той же причине, по которой не годится для `startup.configuration`: сборщики логов развёртывания отбрасывают её тем вероятнее, чем сложнее конфигурация, то есть ровно тогда, когда она нужна. Нумерация делает потерю части строк наблюдаемой.

Значение переменной MUST NOT выводиться ни в каком виде: окружение содержит ключи модели, пароли БД и токены, а состояние `set` несёт всю нужную для диагностики информацию.

Runtime MUST настроить собственный log handler до эмиссии `startup.configuration` и не полагаться на то, что это сделал внешний entrypoint. Запись рождается при сборке приложения, то есть раньше, чем ASGI-сервер настраивает логирование; без собственной настройки она теряется именно в тех развёртываниях, где нужна больше всего.

`capabilities.resolved` MUST различать «сервер не подключён» и «подключён, но каталог пуст»: это разные причины отсутствия тула и разные действия оператора. Неудача подключения MCP-сервера MUST порождать отдельную запись с именем сервера и кодом ошибки, даже когда сервер не является `required` и его пропуск не прерывает run. Запись MUST называть и причину: транспортный код или сообщение нижнего уровня после редактирования. Один общий код без причины не позволяет отличить неверный URL от отказа TLS, недоступного host и отклонённого протокола, а именно этот выбор определяет, что оператору чинить.

## OTLP deployment configuration

Runtime MUST поддерживать стандартные OTLP/HTTP environment variables: общий base endpoint `OTEL_ENDPOINT` и точные per-signal endpoints `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`, `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`. Per-signal value имеет приоритет над общим endpoint и уже является полным URL, поэтому не изменяется.

Общий endpoint MUST выводить только `/v1/traces`. Metrics и logs отправляются исключительно по явно заданным per-signal endpoints. Один base URL не означает, что backend принимает все три сигнала: типовой managed collector принимает трассы и отвечает `403` на логи, а выведенный из base адрес превращает это в постоянный поток ошибок экспорта, который оператор не заказывал и по одному коду не диагностирует. Явный per-signal endpoint является утверждением оператора о том, что этот сигнал там принимают.

`ENABLE_OTEL=false` MUST полностью отключать OTLP-экспорт независимо от заданных endpoints. Instrumentation при этом продолжает работать внутри процесса, а durable audit не затрагивается ни в каком случае.

Имя сервиса берётся из `OTEL_PROJECT_NAME`, а `OTEL_SERVICE_NAME` принимается как синоним с меньшим приоритетом.

Отсутствующий endpoint не отключает instrumentation и не влияет на durable audit.

Local Docker Compose profile MUST запускать version-pinned Arize Phoenix с PostgreSQL persistence в отдельной schema, bounded retention и выключенной Phoenix product telemetry. Core Agent отправляет туда только OTLP traces через `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`; Phoenix UI и HTTP collector доступны на configurable host port, по умолчанию `6006`. Metrics/logs этого development profile могут быть направлены в отдельный collector через соответствующие per-signal variables.

## Context propagation

Core Agent MUST использовать W3C Trace Context через OTel propagators для A2A bindings, MCP HTTP и task queue. Локальные background/subagent процессы получают trace context только через runtime, но trace context не становится authorization.

- `traceparent`/`tracestate` валидируются при extract.
- Входной trace context не даёт authorization или tenant identity.
- Baggage использует allowlist; prompt, user ID, memory text, secrets, paths и tool arguments запрещены.
- Перед внешним недоверенным MCP/A2A endpoint baggage удаляется по default-deny policy.
- Invalid remote context не ломает task и создаёт новый local trace с diagnostic counter.

## Async и background traces

Incoming A2A call MUST NOT создавать отдельный transport/submission trace. Обработка агента сразу начинается span-ом `core_agent.task.execute`: он продолжает валидный incoming W3C parent, а без `traceparent` становится root span нового execution trace. A2A task/context IDs записываются на этом span-е. Transport-only spans `core_agent.a2a.*` и начальный `core_agent.task.submit` не эмитятся.

Независимая background/durable работа, запущенная уже внутри Task, создаёт новый execution trace со `Span Link` на точку запуска. Сабагент не является такой независимой работой и продолжает parent trace.

Сабагент сохраняет trace parent-а, чтобы Phoenix и другой trace UI показывали child `core_agent.task.execute` внутри дерева main agent. Submission/tool span является его прямым parent даже при asynchronous execution; durable recovery сохраняет только W3C trace/span IDs и продолжает тот же trace без authorization/baggage.

Остальные independently scheduled indexing jobs и notification deliveries используют trace links:

- child-agent Task MUST быть child span в trace parent-а;
- independently scheduled/durable operation получает новый trace + link;
- retry создаёт новый attempt span, связанный с logical task/tool call;
- polling/subscription span не становится parent всей Task;
- push notification delivery имеет отдельный producer/client span и link на task execution.

## Обязательные spans

Минимальная span topology:

```text
core_agent.task.execute  (incoming W3C child or local root)
├── core_agent.context.assemble
│   └── core_agent.memory.search
├── gen_ai model operation
├── core_agent.policy.evaluate
├── core_agent.tool.execute
│   ├── core_agent.terminal.session
│   ├── core_agent.terminal.process
│   ├── core_agent.memory.search
│   │   ├── core_agent.memory.search.bm25
│   │   ├── core_agent.memory.search.vector
│   │   ├── core_agent.memory.search.graph
│   │   └── core_agent.memory.search.rerank
│   ├── core_agent.memory.ner
│   ├── core_agent.memory.embed
│   ├── core_agent.memory.index_publish
│   └── mcp operation
└── core_agent.task.checkpoint

core_agent.subagent.execute  (linked parent/child A2A Tasks)
core_agent.notification.deliver
```

Memory spans являются обычными spans агента: подсистема памяти работает в том же процессе, поэтому detailed retrieval/indexing spans создаются внутри того же trace, без cross-process trace context propagation в отдельный сервис.

Операция с duration получает span. Point-in-time transition (`task state changed`, `memory revision published`, `compaction completed`) записывается OTel event/log record с timestamp и безопасными attributes.

## Span attributes

Допустимые high-cardinality IDs на spans/logs, но не metrics: `a2a.task.id`, `a2a.context.id`, `core_agent.run.id`, tool call ID, memory revision, artifact ID и terminal session ID.

Обязательные bounded attributes по применимости:

- service/core version, deployment environment и component;
- operation name, outcome/error type и retry attempt;
- model provider/model capability route и token usage;
- tool namespace/type и policy decision без arguments;
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
- configured effort присутствует в `llm.invocation_parameters`; provider-returned visible reasoning/summary публикуется в `llm.output_messages.<n>.message.contents.<n>.message_content` с `type=reasoning`, а известный usage — в `llm.token_count.completion_details.reasoning`;
- весь фактически доступный модели catalog публикуется как JSON schemas в `llm.tools.<n>.tool.json_schema`; assistant tool calls публикуются в `llm.output_messages.<n>.message.tool_calls.<n>.tool_call.*` с ID, function name и JSON arguments;
- известный model usage публикуется одновременно в OpenInference `llm.token_count.*` и совместимых `gen_ai.usage.*` attributes;
- `core_agent.tool.execute` имеет kind `TOOL`, `tool.name`, description, JSON input/parameters, JSON/text output, outcome и tool-call ID;
- orchestration, policy, checkpoint и protocol spans имеют осмысленный `CHAIN`, а `core_agent.memory.search*`, `*.rerank` и `*.embed` — `RETRIEVER`, `RERANKER` или `EMBEDDING` соответственно;
- успешный span завершается OTel status `OK`, exception — `ERROR` с безопасным exception type без raw error message.

Поля `input.value`, `output.value`, message content, tool schemas/descriptions/arguments/results являются content. Runtime MUST фильтровать их как при создании span, так и при последующем добавлении attributes. Deployment включает их только через `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`. Отсутствующая переменная эквивалентна `false`; local Compose profile явно включает её для ограниченного operator-only Phoenix и документирует retention. Production MUST оставить её выключенной, пока отдельная policy не определит access control, redaction, sampling и retention.

Даже при content capture запрещено экспортировать provider credentials, secret values, provider-hidden/private reasoning, encrypted/redacted thinking, signatures, opaque replay data, MCP authorization headers и полный RunRequest с transport credentials. Явно возвращённый provider-ом visible reasoning/summary разрешён только в reasoning content slot выше и проходит redaction/truncation. System/kernel/profile instructions разрешены только в этом привилегированном operator channel и не становятся A2A output, log или audit content.

## Metrics

Core Agent MUST публиковать минимум:

- A2A request/task count и latency по operation/state/error class;
- active/queued/waiting/background Tasks и queue age;
- model calls, latency, first-token latency, token usage и estimated cost;
- tool/MCP calls, latency, retries, denials и unknown side effects;
- input requested, provided и timed out;
- compaction count, base tokens, working before/after ratio и failures;
- memory tool latency/outcome и configured/filtered state;
- background/subagent count, depth, fan-out, duration и budget usage;
- terminal session/process create/reuse/cleanup latency, resource saturation и policy denials;
- checkpoint/recovery/lease/notification delivery outcomes;
- OTLP export drops/failures и telemetry queue saturation.

Подсистема памяти отдельно MUST публиковать search candidate counts/channels/rerank latency, write validation, 200-line rejections, indexing/NER/entity-resolution latency, revision publication и backlog.

Metric labels MUST иметь bounded cardinality. Task/run/user/tenant IDs, prompt, path, command, entity text, memory ID и raw error message запрещены как labels.

## Logs

- LogRecord содержит timestamp, severity, service/resource, trace ID, span ID, component, safe event name и error code.
- Structured error сохраняет exception type/stack только в защищённом operator channel.
- Debug mode не отключает redaction.
- Notification/message payload, raw stdout/stderr и Markdown content не логируются автоматически; explicit operator content mode MAY писать их bounded/redacted представление в stdout вместе с provider-visible reasoning, но не provider-hidden/opaque reasoning или credentials.
- Tenant data classification определяет exporter, region, retention и access.

## A2A updates и audit

Пользовательская observability идёт через A2A Task status, Messages и Artifact updates. OTel предназначен оператору и не является публичным progress protocol.

Durable audit хранит:

- A2A protocol/extension versions, Message/Task/Artifact revisions;
- kernel/profile/policy versions;
- model routes без hidden reasoning;
- tool intents, safe normalized arguments digests и outcomes;
- background/subagent contracts и notifications;
- memory tool request/result IDs, index revisions и safe mutation outcomes; полный candidate/mutation audit принадлежит подсистеме памяти;
- NER/embedding/reranker versions и index publication;
- compaction mappings, checkpoints, recovery и side-effect reconciliation.

Audit record SHOULD хранить trace/span IDs для перехода от product event к telemetry, но остаётся полным при unsampled trace.

## Sampling и failure behavior

- Head/tail sampling policy задаётся deployment-ом и сохраняет errors, policy denials, high latency и recovery traces в пределах privacy policy.
- Telemetry exporter работает асинхронно с bounded queue и не блокирует agent loop.
- Переполнение telemetry queue создаёт metric/log, но не раскрывает dropped payload.
- Collector outage не меняет Task outcome, кроме deployment-а с обязательным compliance audit; тогда блокируется только действие, для которого audit доказуемо обязателен.

## Evals и quality signals

Versioned eval hooks измеряют task completion, compaction fidelity, hybrid retrieval/rerank quality, NER/entity resolution, tool correctness, false allow/deny, subagent focus, duplicate side effects и cost/latency.

Eval data подчиняется тем же ACL/retention. Evaluation output не становится memory без отдельного validated memory write.
