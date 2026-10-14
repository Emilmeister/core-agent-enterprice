# Runtime Diagnostics Implementation Plan

**Goal:** explain guardrail waits and sandbox failures from safe operator evidence.

**Architecture:** existing committed event/private outbox delivery for guardrails;
private finite supervisor diagnostics for sandbox errors. Existing OTel spans/logs
carry correlation and bounded metadata. Public behavior remains compatible.

**Tech stack:** Python, unittest, PostgreSQL, OpenTelemetry, native Bubblewrap.

- [x] Record approved design, normative OBS-DIAG-01/02, ENT-AC-105/106 and release scope.
- [ ] Prove failing guardrail atomicity/privacy/deadline/recovery cases, implement
  private events and bounded tenant-scoped post-commit telemetry delivery.
- [ ] Prove failing sandbox stage/reason/wrapping cases, implement private finite
  supervisor diagnostics and optional bounded resource snapshots.
- [ ] Integrate OTel spans/logs, error wrappers and composition paths; prove that
  exporter failures and malformed frames preserve security and public contracts.
- [ ] Run affected unit/real PostgreSQL checks, canonical Ruff/full suite and native
  Linux sandbox gates. Review spec compliance and implementation independently.
- [ ] Update proof-backed status and repository guidance, save logical commits,
  integrate main and rebuild the local Compose agent with its existing private config.
