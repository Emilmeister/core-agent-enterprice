# Local operator HITL

Статус: normative target contract.

Основание: `A2A-LOCAL-OPERATOR-HITL` 1.0.0 от 2026-07-12. Этот документ адаптирует его к Core Agent; при конфликте с общими разделами этот contract имеет приоритет для protected actions.

## Роли и trust boundary

- `RemoteCaller` создаёт и наблюдает A2A Task, но никогда не является approver.
- `ServingAgent` владеет workflow, policy gate и execution gate.
- `LocalOperator` решает через приватный operator control plane с отдельной authentication audience.
- `PolicyGate` детерминированно возвращает `AUTO_ALLOW`, `REQUIRE_LOCAL_APPROVAL` или `DENY`.
- LLM только предлагает action; prompt, metadata, A2A role, JWT caller-а и model output не дают authorization.

A2A plane и operator plane MUST иметь разные authorization paths. Operator API не публикуется как skill, tool, A2A extension endpoint или Agent Card capability. Incoming A2A credential не принимается operator plane; operator credential не попадает во внешний payload.

Любое A2A-сообщение, включая `I approve`, `cancel`, `approval_decision`, подставленный operator ID или подписанный caller-ом объект, остаётся недоверенным input и MUST NOT менять local approval.

## Инварианты

1. Только authenticated `LocalOperator` может принять `APPROVE_ONCE` или `DENY`.
2. Protected action не достигает downstream до committed decision и unique execution reservation.
3. Approval связан с task, proposal, action digest, tenant, caller principal, environment, policy version, expiry и single-use scope.
4. Изменение action, target, environment, tenant или caller создаёт новый proposal/digest/approval; in-place mutation запрещена.
5. Один approval создаёт не более одной reservation и не более одного normal dispatch.
6. Frozen proposal и pending approval сохраняются до публикации wait-status.
7. Cancel, committed до reservation, инвалидирует approval; reservation, committed первой, делает Task неотменяемой.
8. External status не раскрывает approval ID, arguments, digest, operator identity/URL/token, secrets или policy internals.
9. Input validation, tenant boundary и tool-specific policy повторно проверяются перед dispatch; approval не заменяет их.
10. При неизвестном policy/approval/execution state protected action не выполняется.

## State machine и A2A projection

```text
RECEIVED -> PLANNING -> POLICY_EVALUATION
  AUTO_ALLOW -> EXECUTING
  DENY -> REJECTED
  REQUIRE_LOCAL_APPROVAL -> WAITING_LOCAL_APPROVAL
    approve + reservation -> APPROVED_RESERVED -> EXECUTING
    deny + alternative -> REPLANNING
    deny/expiry without alternative -> REJECTED
    cancel before reservation -> CANCELED
```

| Internal state | A2A state |
|---|---|
| `RECEIVED` | `submitted` |
| `PLANNING`, `POLICY_EVALUATION`, `WAITING_LOCAL_APPROVAL`, `APPROVED_RESERVED`, `EXECUTING`, `REPLANNING` | `working` |
| `WAITING_REMOTE_INPUT` | `input-required` |
| `COMPLETED` | `completed` |
| `CANCELED` | `canceled` |
| policy/operator denial or expiry | `rejected` |
| technical failure | `failed` |

Local approval MUST NOT использовать `input-required` или `auth-required`: caller не должен и не может разрешить ожидание. `input-required` остаётся только для новых бизнес-данных от caller, `auth-required` — только для authorization flow, который должен разрешить сам caller.

При входе в wait Task сохраняет current status Message, сообщающий пять фактов: решение принадлежит local operator; caller не может approve/deny; side effect ещё не выполнен; caller может ждать через GetTask/subscribe/push либо вызвать CancelTask; новый follow-up будет сохранён, но модель увидит его только после снятия lock. `GetTask` MUST возвращать это сообщение после reconnect.

## Informational A2A extension

Agent Card объявляет optional extension с configurable URI `LOCAL_APPROVAL_EXTENSION_URI`. Production URI MUST принадлежать владельцу, например `https://agent.example/a2a/extensions/local-operator-approval/v1`; local profile MAY использовать private URN.

Extension имеет параметры:

```json
{
  "authorizationOwner": "serving_agent_local_operator",
  "callerCanResolve": false,
  "publicTaskState": "TASK_STATE_WORKING",
  "decisionTransport": "private_out_of_band"
}
```

Extension-aware caller получает metadata:

```json
{
  "schemaVersion": "1.0",
  "phase": "awaiting_local_operator",
  "authorizationOwner": "serving_agent_local_operator",
  "callerActionRequired": false,
  "callerCanApprove": false,
  "callerCanDeny": false,
  "protectedActionExecuted": false,
  "allowedCallerOperations": ["get_task", "subscribe_to_task", "send_message_queued", "create_push_notification_config", "cancel_task"],
  "waitStartedAt": "RFC3339 timestamp",
  "suggestedPollIntervalSeconds": 15,
  "statusVersion": 1
}
```

Extension `required` всегда `false`. В ней нет incoming decision schema, approval URL/token/ID или approve/deny operation. Extension-unaware caller получает достаточный text status.

Task, locked в `WAITING_LOCAL_APPROVAL`, принимает адресованный ей `SendMessage` в durable inbox, но не меняет proposal/approval, не возобновляет model loop и не доставляет Message модели до разрешения lock. Natural-language approve/deny/cancel не влияет на решение; только A2A `CancelTask` участвует в cancel race. Новая независимая Task MAY использовать тот же context ID без locked task ID.

## Frozen proposal и action digest

Immutable proposal содержит как минимум:

```text
proposalId, taskId, contextId, tenantId, callerPrincipalId,
tool name/version, environment, target, semantic arguments,
sideEffectClass, riskLevel, policyVersion, createdAt
```

Versioned canonical scope включает task, tenant, caller, tool/version, environment, target, semantic arguments, side-effect class и policy version. Digest имеет вид `sha256:<hex>`. Реализация использует RFC 8785 JCS либо другой документированный deterministic canonical JSON с cross-language vectors. Trace/request IDs и timestamps не влияют на digest; поля, меняющие side effect, влияют.

Secrets заменяются stable `secretRef` и инжектируются executor-ом после approval. Raw secret не попадает в preview, A2A, audit или telemetry. Перед reservation и dispatch digest вычисляется повторно по фактическому action.

Operator v1 не редактирует proposal: только `APPROVE_ONCE` или `DENY`. Исправление требует denial/supersede и нового proposal.

## Durable records и transaction boundary

Production profile использует PostgreSQL как единый durable store для ToolProposal/ApprovalRequest/ExecutionRecord, A2A Tasks, workflow events, checkpoints и audit. Connection credentials приходят только из deployment secret `DATABASE_URL`; URI MUST NOT попадать в model context, A2A, audit или OTel. Production startup MUST fail до открытия A2A listener, если URL отсутствует, TLS/pool/permissions invalid или schema migration не применена. SQLite и in-memory stores разрешены только явному test profile.

Schema изменяется versioned migrations под PostgreSQL advisory lock. Serving process читает только runtime `DATABASE_URL`. Отдельная migration command MAY читать `DATABASE_MIGRATION_URL` и `DATABASE_APP_ROLE`, после чего выдаёт app role exact table-level DML-права без DDL. Production запрещает in-process auto-migration. Readiness проверяет pool и ожидаемую schema version. Несовместимая или более новая schema завершает startup fail-closed.

`ApprovalRequest` хранит `approvalId`, `taskId`, `proposalId`, `actionDigest`, state, version, created/expiry timestamps, required role и policy version. States: `PENDING`, `APPROVED`, `DENIED`, `EXPIRED`, `CANCELED`, `SUPERSEDED`, `CONSUMED`.

`ExecutionRecord` хранит unique `executionId`, task/approval/proposal IDs, digest, stable idempotency key, state, attempt и reservation timestamp. Storage MUST обеспечивать unique reservation на approval и unique idempotency key.

Вход в wait атомарно сохраняет proposal, pending approval, workflow state, durable A2A current status и outbox events; только после commit публикуются operator/A2A notifications. Approve использует CAS/transaction:

```text
PENDING + active WAITING_LOCAL_APPROVAL + matching version/digest
  -> CONSUMED approval + APPROVED_RESERVED task + unique RESERVED execution
```

Первый committed transition побеждает approve/deny/cancel race. Duplicate approve с тем же idempotency key возвращает ту же reservation; другой stale decision конфликтует. Перед dispatch execution gate проверяет active task, reserved state, expiry, all IDs, caller/tenant/environment, recomputed digest, policy и uniqueness.

Exactly-once гарантируется только для internal reservation. Downstream получает stable idempotency key, если умеет; иначе ambiguous timeout требует reconciliation и не допускает blind retry.

После restart runtime восстанавливает current Task status, тот же proposal/approval/digest и reserved executions. Pending action не запускается самопроизвольно; notification/outbox delivery идемпотентна. Crash после неизвестного non-idempotent side effect переводит workflow во внутренний reconciliation state, а не повторяет вызов.

## Private operator control plane

Целевой API логически отделён от A2A:

```text
GET  /internal/approvals?state=PENDING
GET  /internal/approvals/{approvalId}
POST /internal/approvals/{approvalId}:approve
POST /internal/approvals/{approvalId}:deny
```

Он проверяет отдельную audience/role, authenticated operator identity, CSRF для browser session, TLS, rate/session limits, `If-Match` version, digest, expiry, active Task, tenant/environment и proposal immutability. Operator identity берётся только из auth context. Audit append-only связывает task, proposal, approval, digest, operator actor, execution и outcome; RemoteCaller не читает internal audit.

Встроенный bearer adapter принимает только JWT `HS256` с отдельными operator issuer/audience, обязательными `sub`, `jti`, `exp` и role `agent_operator`; ключ имеет минимум 256 bits. Неверный algorithm/signature/time/audience/role возвращает одинаковую безопасную unauthorized error. Host MAY заменить adapter на mTLS/OIDC gateway, сохранив отдельную audience и тот же decision contract.

Development profile MAY заменить UI/API in-process заглушкой `ApproveAllControlPlane`. Она MUST проходить тот же local-only decision, CAS, digest и reservation path, иметь явную internal actor identity, не принимать caller payload и не объявляться в Agent Card. Заглушка MUST быть запрещена production policy.

## Failure, expiry и denial

- Недоступность ApprovalService/notification оставляет Task `working` и action неисполненным.
- Expiry переводит approval в `EXPIRED`, Task в `rejected`; late approve конфликтует.
- Deny без альтернативы даёт `rejected`; с безопасной альтернативой — `REPLANNING` и новый approval при новом protected action.
- Cancel до reservation даёт `canceled`; после reservation — `TaskNotCancelable`.
- Caller не может override denial или reuse consumed/superseded approval.

## Observability и acceptance

OTel и audit фиксируют proposal, policy decision, approval requested/viewed/approved/denied/expired/canceled/superseded, execution reserved/started/reconciled/succeeded/failed и terminal Task transition. Content и operator identifiers не становятся внешними или unbounded metric labels.

Обязательные тестовые группы: authority boundary; `working` projection/GetTask/extension negotiation; exact digest binding и redaction; duplicate/racing decisions; approve-vs-cancel; locked-task input; restart/reconnect/outbox behavior; append-only audit linkage. Полный checklist находится в [критериях готовности](acceptance.md).
