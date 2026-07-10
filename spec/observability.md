# Наблюдаемость

## Принцип

Пользователь должен понимать, что делает агент, не получая скрытую chain-of-thought и секреты. Оператор должен восстановить технический ход запуска из аудита.

## События для клиента

Минимальные payloads:

| Event | Обязательные данные |
|---|---|
| `run.started` | принятая версия контракта, names skills/MCP, effective limits |
| `assistant.delta` | очередной фрагмент пользовательского текста |
| `assistant.message` | завершённое пользовательское сообщение |
| `tool.requested` | tool_call_id, tool name, безопасное summary |
| `approval.required` | approval_id, точный effect, risks, redacted arguments |
| `approval.resolved` | approval_id, decision, resolver type |
| `input.required` | input_id, user-facing question, response schema, timeout |
| `input.resolved` | input_id, resolver type |
| `tool.started` | tool_call_id, attempt |
| `tool.completed` | status, duration, result preview, artifact reference |
| `tool.failed` | безопасный error code, retryable |
| `context.compacted` | compaction number, before/after token count, replaced sequence ranges |
| `memory.updated` | memory id, scope, operation и provenance без sensitive content |
| `checkpoint.created` | checkpoint id, run revision, reason |
| `subrun.started/completed/failed` | child run id, contract status и usage |
| `run.paused/resumed/recovering` | run revision и безопасная причина |
| `run.completed` | итоговый message и usage |
| `run.failed` | error code, safe message, correlation_id |
| `run.cancelled` | reason и известные незавершённые side effects |
| `run.aborted` | safe reason, reconciliation status и известные side effects |

События tool и approval MUST NOT включать raw secret values или скрытые reasoning data.

## Audit transcript

Audit хранит append-only записи:

- исходный RunRequest после redaction;
- snapshot manifests MCP и skills с digests;
- model request/response metadata без raw reasoning;
- tool intent, нормализованные arguments после redaction и results;
- approvals и identity resolver-а, если её предоставляет host;
- human inputs, grants, revocations и policy versions;
- compaction mapping;
- memory retrieval candidates, выбранные records и memory mutations;
- model routes, fallbacks, checkpoints, leases и recovery decisions;
- child run contracts и результаты;
- terminal state и usage.

Audit sequence MUST совпадать с публичной event sequence либо содержать однозначное отображение на неё.

## Артефакты

Большие tool outputs хранятся отдельно. Artifact reference содержит:

```json
{
  "artifact_id": "art_01...",
  "media_type": "text/plain",
  "bytes": 123456,
  "sha256": "...",
  "truncated_in_context": true
}
```

Доступ к artifact применяет те же authorization и retention policy, что доступ к run.

## Метрики

Ядро MUST публиковать агрегируемые метрики:

- runs по terminal status и error code;
- latency запуска и model calls;
- input/output/reasoning token usage, если provider возвращает её;
- число и latency tool calls по namespace;
- approvals requested/approved/denied/timed out;
- compaction count и before/after occupancy;
- retries и MCP disconnects;
- sandbox/policy denials;
- estimated cost, если доступна.
- queue/lease/checkpoint/recovery latency и outcomes;
- memory retrieval/write/delete и cache invalidation;
- child run depth, fan-out, latency и budget usage;
- model route/fallback outcomes по ограниченному route class.

Labels MUST иметь ограниченную cardinality. `run_id`, prompt, file path, command и tool arguments запрещены как metric labels.

## Логи

- Structured logs содержат `run_id`, `correlation_id`, component, event type и safe status.
- Prompt и tool output не логируются по умолчанию.
- Debug logging не может отключить secret redaction.
- Пользовательский event stream и operator logs являются разными интерфейсами и имеют разные права доступа.

## Distributed tracing

Один trace связывает API adapter, orchestrator, model, tool, MCP, policy, store и child runs. Span attributes проходят redaction и используют bounded labels. Trace context MAY передаваться MCP server только если tenant policy разрешает раскрытие correlation metadata.

## Evals и quality signals

Ядро предоставляет hooks для offline replay и online evaluation без включения пользовательских данных по умолчанию. Минимальные quality dimensions:

- task completion и проверяемость результата;
- сохранность goal/constraints после compaction;
- корректность tool selection и arguments;
- false allow/false deny policy decisions;
- лишние approvals и вопросы пользователю;
- memory precision, provenance и harmful stale retrieval;
- recovery без duplicate side effects;
- cost/latency относительно результата.

Eval datasets версионируются и применяют ту же data classification/retention policy. Evaluation model output не становится частью пользовательской memory.
