# Трассируемость реализации v1

Этот файл является release gate, а не заменой нормативных документов. Статус `implemented` допустим только при наличии автоматического доказательства в обычном CI. `partial` и `missing` блокируют production release, даже если happy-path demo работает локально.

## Статусы

- `implemented` — contract, failure modes и restart/race behavior проверяются автоматически;
- `partial` — существует часть поведения, но отсутствует обязательная гарантия или CI proof;
- `missing` — production path отсутствует.

## Существующие foundations и изменённые контракты

| ID | Требование v1 | Доказательство | Статус |
|---|---|---|---|
| A2A-01 | Agent Card и version negotiation без собственного расширения протокола | `test_a2a_config`, `test_advanced_protocols` | implemented |
| A2A-02 | send/stream/get/list/subscribe/cancel используют одну durable Task | active/passive subscription ordering, lease-loss, late-cancel и PostgreSQL terminal reconciliation tests; enterprise изменённая семантика требует нового CI proof | partial |
| A2A-03 | push notification at-least-once и deduplication | encrypted push retry/restart и atomic recovery reconciliation tests; enterprise изменённая семантика требует нового CI proof | partial |
| A2A-04 | active Task принимает durable/idempotent follow-up Messages и доставляет их model loop на safe boundary без completion race | runtime concurrency, A2A HTTP E2E и PostgreSQL inbox/completion-gate tests; enterprise изменённая семантика требует нового CI proof | partial |
| A2A-05 | streaming публикует reasoning, function_call/function_response и кумулятивные partial-снимки, ровно один terminal statusUpdate, а partial-кадры не попадают в durable history и push | ADK stream shape E2E, transient-history и push suppression tests; enterprise изменённая семантика требует нового CI proof | partial |
| CFG-01 | Platform/Agent/Run разделены, capability intersection fail closed | effective-config contract suite | implemented |
| CFG-03 | deployment variables разбираются по фиксированному контракту, а неизвестное или конфликтующее значение fail closed | configuration transfer suite; enterprise изменённая семантика требует нового CI proof | partial |
| CFG-02 | disabled capability отсутствует в card/catalog и stale call denied | built-in allowlist/runtime-mode card and model tests; enterprise изменённая семантика требует нового CI proof | partial |
| KRN-01 | versioned kernel отдельно от profile, runtime enforcement | protected kernel persistence test | implemented |
| KRN-02 | child наследует kernel и не расширяет policy | delegation E2E and exact catalog tests | implemented |
| KRN-03 | пустой profile не дублирует user prompt; conditional prompt/tool guidance не обещает отсутствующие capabilities | kernel and built-in description contract tests | implemented |
| RUN-01 | сериализуемая state machine и irreversible terminal states | workflow transition suite | implemented |
| RUN-02 | lease heartbeat, checkpoint replay и recovery безопасной границы | automatic root recovery, graceful shutdown, single-attempt lost claim, live-lease exclusion, post-lock final fencing and PostgreSQL process-state-loss tests; PostgreSQL long model/tool/join proof отсутствует | partial |
| RUN-04 | hard budget резервирует финальный turn; каждый provider/tool attempt атомарно учитывается, exhausted tools не dispatch-ятся, а truthful partial result завершается с `complete=false` | retry/crash markers, atomic PostgreSQL model/tool charges, shared ledger/admission and live/recovered A2A partial tests | implemented |
| RUN-03 | ambiguous side effect переходит в reconciliation без retry или cancel masking | mutating exception/resume/cancel regressions and dispatched-side-effect restart chaos test | implemented |
| CTX-01 | base/working budget и compaction 90% до 10–15% | context budget boundary tests | implemented |
| CTX-02 | pinned state и transcript provenance переживают compaction/restart | two-compaction runtime test; enterprise изменённая семантика требует нового CI proof | partial |
| CTX-03 | крупный tool result offload-ится из active context без потери immutable transcript | in-memory artifact/excerpt runtime test; PostgreSQL restart proof отсутствует | partial |
| SKL-01 | модель семантически подключает только effective skills, поэтапно получает `SKILL.md` и ограниченные ресурсы immutable package; служебные вызовы учитываются общим budget и выводятся для child из exact skill allowlist | effective-config gate and reserved-name collision; resolver lock/security; activation-boundary crash, streaming, legacy snapshot and context-budget runtime tests; delegation E2E; vendored package, Compose and image smoke tests | implemented |
| MEM-01 | built-in `core_memory_*` подсистемы Core Agent: Markdown schema, optimistic revisions и hard 200-line rejection как recoverable tool result | `test_memory_service`; новый stable caller scope требует CI proof | partial |
| MEM-02 | atomic BM25/vector/NER/graph publication и stale edge removal внутри процесса агента | `test_memory_service` + real PostgreSQL CI suite; новый stable caller scope требует CI proof | partial |
| MEM-03 | hybrid candidates, calibrated fusion, provenance rerank и degraded channels вместо отказа | `test_memory_service`; новый stable caller scope требует CI proof | partial |
| BGT-01 | background Tasks и mailbox durable, versioned, at-least-once | PostgreSQL recovery/mailbox test | implemented |
| BGT-02 | passive wait освобождает worker; recursive cancel останавливает child agent loop и process tree | scheduler, child/grandchild safe-boundary and PTY process-group cancel tests | partial |
| BGT-03 | durable scheduler recovery использует server-clock expiry-fenced claim/heartbeat, сохраняет late result и не маскирует неизвестную мутацию cancel state-ом | concurrent/expired/skewed-clock и row-lock-wait fencing, scan/claim/post-claim cancel races, distributed cancel/restart и `SIDE_EFFECT_UNKNOWN` PostgreSQL tests | implemented |
| SUB-01 | child является durable Task с одним ID, exact capabilities/shared memory policy | stable-ID delegation and child-memory E2E | partial |
| SUB-02 | общий parent budget, hard depth `2`/fan-out и обычный child text result | depth-2 catalog/kernel E2E, stale-call guard and atomic budget/fan-out tests | implemented |
| SUB-03 | balanced decision rule выбирает materially useful parallel/isolated/verifiable outcome, сохраняет bounded method autonomy при exact capabilities и требует оба положительных child budget limits | delegation kernel/description/schema/runtime validation contract tests | implemented |
| ART-01 | Dedicated artifact tools удалены; входящие files и A2A results работают, legacy blobs сохранены | прежняя artifact suite доказывает удаляемое поведение; enterprise proof отсутствует | missing |
| RMT-01 | Owner remote registry, server-held auth headers и asynchronous handle/IDs с final wait timeout | прежние synchronous/env/forwarded-header tests не доказывают новый контракт | partial |
| MCP-01 | новый запрос durable ожидает transient cold start Streamable HTTP MCP до общего для run deadline, изолирует и очищает session, различает permanent failures и не расходует model/tool budget | transport/config, runtime и A2A E2E tests; PostgreSQL restart, cross-worker cancel, reconnect catalog/deadline, required failure и atomic terminal-disposition tests | implemented |
| TER-01 | owned PTY/process groups/workspaces, bounded output и основной CLI-набор | local terminal integration suite, model-facing description contract и Docker CLI smoke; enterprise изменённая семантика требует нового CI proof | partial |
| TER-02 | immutable base snapshot, conflict-aware merge и S3 manifest | content-addressed snapshot/merge tests; enterprise изменённая семантика требует нового CI proof | partial |
| PY-01 | bounded Python process вызывает exact built-in/MCP tools через policy/budget/audit/OTel broker | Python broker E2E, mode gate and process failure tests; enterprise изменённая семантика требует нового CI proof | partial |
| DB-01 | PostgreSQL schema/pool/migration role/readiness без fallback | real PostgreSQL CI suite | implemented |
| DB-02 | все production rows tenant-scoped и cross-tenant not-found | tenant isolation and artifact tests; enterprise изменённая семантика требует нового CI proof | partial |
| OTL-01 | OTLP traces/metrics/logs, one A2A execution trace without transport/submission trace, per-signal routing и W3C propagation | OTLP HTTP/per-signal collector, Compose contract and A2A/MCP propagation tests | implemented |
| OTL-02 | bounded labels/content-off и exporter failure isolation | telemetry privacy/failure suite | implemented |
| OTL-03 | Phoenix отображает OpenInference AGENT/LLM/TOOL, prompt/catalog/calls/results/usage и provider-visible reasoning при explicit opt-in | OpenInference reasoning/attribute/status contract tests, Compose capture contract and live Phoenix smoke | implemented |
| SEC-01 | stable public errors, secret redaction и trust boundaries | adversarial/security suite and secret scan gate; enterprise изменённая семантика требует нового CI proof | partial |
| SEC-02 | retention/coordinated deletion очищает cached derived data | run-family lifecycle and Memory delete E2E; enterprise изменённая семантика требует нового CI proof | partial |
| OPS-01 | non-root image, writable durable mounts, graceful shutdown, probes и separate migration | pinned image/mount smoke, Compose init, health/migration gates | implemented |
| CI-01 | spec, unit, PostgreSQL, E2E, crash/race и image gates обязательны | `.github/workflows/ci.yml` | implemented |

## Enterprise traceability

Каждый ENT-AC соответствует одному сценарию [acceptance](acceptance.md). Новый контракт получает `implemented` только после обычного CI proof; foundations выше не являются доказательством UI/HITL/sandbox/cron. `missing` ниже означает отсутствие полного production path и его доказательства, а не отсутствие каждого вспомогательного primitive.

| ID | Требование / normative source | Доказательство | Статус |
|---|---|---|---|
| ENT-AC-01 | [Совместный owner UI](product.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-02 | [Owner-only решения](security-and-reliability.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-03 | [Caller isolation](security-and-reliability.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-04 | [Introspection fail closed](security-and-reliability.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | `tests.test_auth` invalid/expired/revoked claims, outage и startup; `tests.test_keycloak_integration` реальный Keycloak в CI | implemented |
| ENT-AC-05 | [Стабильная identity](security-and-reliability.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | `tests.test_auth` replacement token и прежняя Task на memory/PostgreSQL; `tests.test_keycloak_integration` реальные service-account tokens | implemented |
| ENT-AC-06 | [Live tool deny](tools.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-07 | [Default HITL](tools.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-08 | [Exact call approval](runtime.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-09 | [HITL отказ/timeout](runtime.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-10 | [HITL recovery](runtime.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-11 | [Общий inbound лимит](artifacts.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-12 | [Path safety](artifacts.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-13 | [Download scope](artifacts.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-14 | [Atomic root admission](public-contract.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | `tests.test_admission`: конкурентные root и независимые PostgreSQL pools, persisted busy Task, rollback, shutdown/restart и disconnect recovery | implemented |
| ENT-AC-15 | [Busy во время ожидания](public-contract.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | `tests.test_admission` подтверждает удержание слота canonical nonterminal state; реальные enterprise HITL/remote ожидания ещё не подключены | partial |
| ENT-AC-16 | [Follow-up safe boundary](runtime.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-17 | [Follow-up dedup/race](runtime.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-17a | [Follow-up during HITL/remote wait](runtime.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-18 | [Remote recovery](tasks-and-delegation.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-19 | [Final remote timeout](tasks-and-delegation.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-20 | [Remote outcome race](tasks-and-delegation.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-21 | [Timer recovery](tasks-and-delegation.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-22 | [Cron overlap](tasks-and-delegation.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-23 | [Cron tool/UI policy](tasks-and-delegation.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-24 | [Cron context continuity](context.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-25 | [Semantic corrections](context.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-26 | [Structured authoritative state](context.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-27 | [Guardrails material gate](security-and-reliability.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-28 | [Artifact tools removal](artifacts.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-29 | [Concurrent sandbox files](execution-environment.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-30 | [Process/broker/secrets isolation](execution-environment.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-31 | [Sandbox fail closed](execution-environment.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-32 | [Persistent chat folder](execution-environment.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-33 | [Public egress](execution-environment.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-34 | [Private network denied](execution-environment.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-35 | [IPv4/IPv6 egress](execution-environment.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-36 | [Network control fail closed](execution-environment.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-37 | [Guardrail reject](security-and-reliability.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-38 | [Quarantine across retrieval](security-and-reliability.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-39 | [Guardrail timeout](security-and-reliability.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-40 | [Guardrail restart/race](security-and-reliability.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-41 | [Detector unavailable](security-and-reliability.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-42 | [Owner tool exemption](tools.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-43 | [Exemption authority](tools.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-44 | [Exemption vs outage](tools.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-45 | [Classifier integration](security-and-reliability.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-46 | [Pending HITL vs auto](runtime.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-47 | [Owner-private projection](a2a-protocol.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-48 | [Follow-up vs owner answer](runtime.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-49 | [Owner-answer deadline](runtime.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-50 | [Late remote result](tasks-and-delegation.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-51 | [Deny closes pending calls](runtime.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-52 | [Cron downtime skips](tasks-and-delegation.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-53 | [Cron disable/delete](tasks-and-delegation.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-54 | [Cron timezone](tasks-and-delegation.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-55 | [Cron run now](tasks-and-delegation.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-56 | [Manual/cron admission race](tasks-and-delegation.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-57 | [Cron parameter snapshot](tasks-and-delegation.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-58 | [Manual file cleanup](artifacts.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-59 | [Cleanup revision/restart](artifacts.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-60 | [Age preview filter](artifacts.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-61 | [Busy cleanup exclusion](artifacts.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-62 | [Filename collisions](artifacts.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-63 | [Atomic attachment admission](artifacts.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-64 | [Outbound total limit](artifacts.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-65 | [Indefinite history](context.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-66 | [Stable-caller creation dedup](public-contract.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | `tests.test_admission`: actor/messageId, canonical Message, конфликт, restart, смена токена, busy и completed duplicates; файловый admission пока отклонён | partial |
| ENT-AC-67 | [HTTP request auth boundary](security-and-reliability.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | `tests.test_auth` stream завершается после expiry во время generation, следующий запрос отклонён; memory/PostgreSQL | implemented |
| ENT-AC-68 | [Orphan upload cleanup](artifacts.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-69 | [Message wakes timer](tasks-and-delegation.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-AC-70 | [Wait generation/dedup](tasks-and-delegation.md), сценарий [acceptance](acceptance.md#enterprise-v1-обязательные-сценарии) | enterprise CI proof отсутствует | missing |
| ENT-MIG-01 | Versioned schema, explicit identity/file/remote config migration и rollback boundary по architecture | migration/restart/rollback proof отсутствует | missing |

## Release rule

Production release v1 разрешён только когда все строки, относящиеся к scope `releases/v1.md`, имеют статус `implemented`, а полный test command воспроизводится из чистого checkout без ручных шагов. Target-only exclusions остаются `partial`/`missing` только если явно перечислены в release profile.
