# Runtime Diagnostics Implementation Plan

**Goal:** explain guardrail waits and sandbox failures from safe operator evidence.

**Architecture:** existing committed event/private outbox delivery for guardrails;
private finite supervisor diagnostics for sandbox errors. Existing OTel spans/logs
carry correlation and bounded metadata. Public behavior remains compatible.

**Tech stack:** Python, unittest, PostgreSQL, OpenTelemetry, native Bubblewrap.

- [x] Record approved design, normative OBS-DIAG-01/02, ENT-AC-105/106 and release scope.
- [x] Prove failing guardrail atomicity/privacy/deadline/recovery cases, implement
  private events and bounded tenant-scoped post-commit telemetry delivery.
- [x] Prove failing sandbox stage/reason/wrapping cases, implement private finite
  supervisor diagnostics and optional bounded resource snapshots.
- [x] Integrate OTel spans/logs, error wrappers and composition paths; prove that
  exporter failures and malformed frames preserve security and public contracts.
- [x] Run affected unit/real PostgreSQL checks, canonical Ruff/full suite and native
  Linux sandbox gates. Review spec compliance and implementation independently.
- [x] Update proof-backed status and repository guidance, save logical commits,
  integrate main and rebuild the local Compose agent with its existing private config.

Verification: canonical Ruff and 2,049 ordinary tests passed (three fixture/platform
skips); required native ARM64 sandbox (12 tests) and native Compose composition
(1 test) passed without skips on the final tested image. Independent reviews
closed the active-memory Task projection rollback defect.

Native amd64 validation remains required on a real runner before claiming the
complete multi-architecture release proof; emulation was not used. No original
sandbox failure is assigned a cause without an instrumented reproduction.

Local rollout completed: main contains the reviewed change, Compose uses the
final tested native arm64 image, and readiness, owner UI and Keycloak discovery
return HTTP 200. Existing application/Keycloak volumes and private settings were
preserved. Disposable test databases, Keycloak and Linux fixtures were removed.
