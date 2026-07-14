# Трассируемость реализации v1

Этот файл является release gate, а не заменой нормативных документов. Статус `implemented` допустим только при наличии автоматического доказательства в обычном CI. `partial` и `missing` блокируют production release, даже если happy-path demo работает локально.

## Статусы

- `implemented` — contract, failure modes и restart/race behavior проверяются автоматически;
- `partial` — существует часть поведения, но отсутствует обязательная гарантия или CI proof;
- `missing` — production path отсутствует.

## Матрица

| ID | Требование v1 | Доказательство | Статус |
|---|---|---|---|
| A2A-01 | Agent Card, Core extension и version negotiation | `test_a2a_config`, `test_advanced_protocols` | implemented |
| A2A-02 | send/stream/get/list/subscribe/cancel используют одну durable Task | `test_end_to_end`, PostgreSQL A2A reconciliation | implemented |
| A2A-03 | push notification at-least-once и deduplication | encrypted push retry/restart integration | implemented |
| A2A-04 | active Task принимает durable/idempotent follow-up Messages и доставляет их model loop на safe boundary без completion race | runtime concurrency, A2A HTTP E2E и PostgreSQL inbox/completion-gate tests | implemented |
| CFG-01 | Platform/Agent/Run разделены, capability intersection fail closed | effective-config contract suite | implemented |
| CFG-02 | disabled capability отсутствует в card/catalog и stale call denied | built-in allowlist/runtime-mode card and model tests | implemented |
| KRN-01 | versioned kernel отдельно от profile, runtime enforcement | protected kernel persistence test | implemented |
| KRN-02 | child наследует kernel и не расширяет policy | delegation E2E and exact catalog tests | implemented |
| KRN-03 | пустой profile не дублирует user prompt; conditional prompt/tool guidance не обещает отсутствующие capabilities | kernel and built-in description contract tests | implemented |
| RUN-01 | сериализуемая state machine и irreversible terminal states | workflow transition suite | implemented |
| RUN-02 | lease, checkpoint replay и recovery безопасной границы | PostgreSQL process-state-loss tests | implemented |
| RUN-03 | ambiguous side effect переходит в reconciliation без retry | dispatched-side-effect restart chaos test | implemented |
| CTX-01 | base/working budget и compaction 90% до 10–15% | context budget boundary tests | implemented |
| CTX-02 | pinned state и transcript provenance переживают compaction/restart | two-compaction runtime test | implemented |
| MEM-01 | Markdown schema, optimistic revisions и hard 200-line rejection | Memory MCP limit/concurrency suite | implemented |
| MEM-02 | atomic BM25/vector/NER/graph publication и stale edge removal | committed revision restart/failure/rebuild tests | implemented |
| MEM-03 | hybrid candidates, calibrated fusion и provenance rerank | hybrid retrieval and provider tests | implemented |
| BGT-01 | background Tasks и mailbox durable, versioned, at-least-once | PostgreSQL recovery/mailbox test | implemented |
| BGT-02 | passive wait освобождает worker и cancel handles process tree | scheduler and PTY process-group cancel tests | implemented |
| SUB-01 | child является durable Task с exact capabilities/shared memory policy | delegation and child-memory E2E | implemented |
| SUB-02 | общий parent budget, hard depth `2`/fan-out и обычный child text result | depth-2 catalog/kernel E2E, stale-call guard and atomic budget/fan-out tests | implemented |
| SUB-03 | outcome contract сохраняет bounded method autonomy при minimum sufficient/exact capabilities | delegation kernel/description contract tests | implemented |
| TER-01 | owned PTY/process groups/workspaces и bounded output | local terminal integration suite | implemented |
| TER-02 | immutable base snapshot, conflict-aware merge и S3 manifest | content-addressed snapshot/merge tests | implemented |
| PY-01 | bounded Python process вызывает exact built-in/MCP tools через policy/budget/audit/OTel broker только без HITL | Python broker E2E, mode gate and process failure tests | implemented |
| HITL-01 | private authority, frozen digest и single reservation | approval digest/race suite | implemented |
| HITL-02 | wait transition, A2A status, checkpoint, audit и outbox atomic | PostgreSQL HITL continuation/reconciliation tests | implemented |
| HITL-03 | approval/rejection/cancel/recovery сохраняют IDs и at-most-once | operator A2A and crash-at-dispatch tests | implemented |
| CTL-01 | production private operator API с отдельной auth audience | operator JWT authority E2E | implemented |
| DB-01 | PostgreSQL schema/pool/migration role/readiness без fallback | real PostgreSQL CI suite | implemented |
| DB-02 | все production rows tenant-scoped и cross-tenant not-found | tenant isolation and artifact tests | implemented |
| OTL-01 | OTLP traces/metrics/logs, one A2A execution trace without transport/submission trace, per-signal routing и W3C propagation | OTLP HTTP/per-signal collector, Compose contract and A2A/MCP propagation tests | implemented |
| OTL-02 | bounded labels/content-off и exporter failure isolation | telemetry privacy/failure suite | implemented |
| OTL-03 | Phoenix отображает OpenInference AGENT/LLM/TOOL, prompt/catalog/calls/results/usage и provider-visible reasoning при explicit opt-in | OpenInference reasoning/attribute/status contract tests, Compose capture contract and live Phoenix smoke | implemented |
| SEC-01 | stable public errors, secret redaction и trust boundaries | adversarial/security suite and secret scan gate | implemented |
| SEC-02 | retention/coordinated deletion очищает cached derived data | run-family lifecycle and Memory delete E2E | implemented |
| OPS-01 | non-root image, writable durable mounts, graceful shutdown, probes и separate migration | pinned image/mount smoke, Compose init, health/migration gates | implemented |
| CI-01 | spec, unit, PostgreSQL, E2E, crash/race и image gates обязательны | `.github/workflows/ci.yml` | implemented |

## Release rule

Production release v1 разрешён только когда все строки, относящиеся к scope `releases/v1.md`, имеют статус `implemented`, а полный test command воспроизводится из чистого checkout без ручных шагов. Target-only exclusions остаются `partial`/`missing` только если явно перечислены в release profile.
