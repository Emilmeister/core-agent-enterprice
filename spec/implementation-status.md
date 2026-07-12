# Трассируемость реализации v1

Этот файл является release gate, а не заменой нормативных документов. Статус `implemented` допустим только при наличии автоматического доказательства в обычном CI. `partial` и `missing` блокируют production release, даже если happy-path demo работает локально.

## Статусы

- `implemented` — contract, failure modes и restart/race behavior проверяются автоматически;
- `partial` — существует часть поведения, но отсутствует обязательная гарантия или CI proof;
- `missing` — production path отсутствует.

## Матрица

| ID | Требование v1 | Доказательство | Статус |
|---|---|---|---|
| A2A-01 | Agent Card, Core extension и version negotiation | protocol contract tests | partial |
| A2A-02 | send/stream/get/list/subscribe/cancel используют одну durable Task | A2A disconnect/reconnect E2E | partial |
| A2A-03 | push notification at-least-once и deduplication | webhook integration test | missing |
| CFG-01 | Platform/Agent/Run разделены, capability intersection fail closed | config contract tests | partial |
| CFG-02 | disabled capability отсутствует в card/catalog и stale call denied | discovery E2E | partial |
| KRN-01 | versioned kernel отдельно от profile, runtime enforcement | prompt-injection tests | partial |
| KRN-02 | child наследует kernel и не расширяет policy | delegation security E2E | partial |
| RUN-01 | сериализуемая state machine и irreversible terminal states | workflow transition tests | missing |
| RUN-02 | lease, checkpoint replay и recovery безопасной границы | kill/restart E2E | missing |
| RUN-03 | ambiguous side effect переходит в reconciliation без retry | crash-at-dispatch chaos test | missing |
| CTX-01 | base/working budget и compaction 90% до 10–15% | tokenizer boundary tests | partial |
| CTX-02 | pinned state и transcript provenance переживают compaction/restart | two-compaction E2E | partial |
| MEM-01 | Markdown schema, optimistic revisions и hard 200-line rejection | Memory MCP tests | partial |
| MEM-02 | atomic BM25/vector/NER/graph publication и stale edge removal | rebuild/failure tests | partial |
| MEM-03 | hybrid candidates, calibrated fusion и provenance rerank | retrieval quality tests | partial |
| BGT-01 | background Tasks и mailbox durable, versioned, at-least-once | restart notification E2E | missing |
| BGT-02 | passive wait освобождает worker и cancel handles process tree | concurrency/cancel E2E | partial |
| SUB-01 | child является durable Task с exact capabilities/shared memory policy | delegation E2E | partial |
| SUB-02 | общий parent budget, depth/fan-out и schema-valid result | budget/result tests | partial |
| TER-01 | owned PTY/process groups/workspaces и bounded output | terminal integration tests | partial |
| TER-02 | immutable base snapshot, conflict-aware merge и S3 manifest | workspace merge E2E | missing |
| HITL-01 | private authority, frozen digest и single reservation | approval race tests | partial |
| HITL-02 | wait transition, A2A status, checkpoint, audit и outbox atomic | transaction/crash E2E | missing |
| HITL-03 | approval/rejection/cancel/recovery сохраняют IDs и at-most-once | restart/race chaos tests | missing |
| CTL-01 | production private operator API с отдельной auth audience | control-plane security E2E | missing |
| DB-01 | PostgreSQL schema/pool/migration role/readiness без fallback | PostgreSQL integration tests | partial |
| DB-02 | все production rows tenant-scoped и cross-tenant not-found | tenant isolation tests | partial |
| OTL-01 | OTLP traces/metrics/logs и W3C propagation | collector integration test | partial |
| OTL-02 | bounded labels/content-off и exporter failure isolation | telemetry privacy/failure tests | partial |
| SEC-01 | stable public errors, secret redaction и trust boundaries | adversarial/security tests | partial |
| SEC-02 | retention/coordinated deletion очищает cached derived data | lifecycle E2E | missing |
| OPS-01 | non-root image, graceful shutdown, probes и separate migration | container smoke/termination tests | partial |
| CI-01 | spec, unit, PostgreSQL, E2E, crash/race и image gates обязательны | CI workflow | missing |

## Release rule

Production release v1 разрешён только когда все строки, относящиеся к scope `releases/v1.md`, имеют статус `implemented`, а полный test command воспроизводится из чистого checkout без ручных шагов. Target-only exclusions остаются `partial`/`missing` только если явно перечислены в release profile.
