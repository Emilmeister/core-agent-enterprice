# Guardrail and sandbox diagnostics

Approved scope: explain committed guardrail outcomes and sandbox launch failures
in existing operator telemetry. Business/model behavior and benchmark changes
are outside this change.

Guardrail classification, wait entry/resolution and continuation have stable
private diagnostic events, recorded with existing durable state. Private outbox
delivery is bounded and tenant-scoped. Post-commit logs/spans show enum outcomes,
timing, counters and separate detector/owner deadlines. No payload, digest,
arguments, paths, owner answers or raw errors enter these events. Export failure
does not roll back work; replay preserves event identity without promising
exactly-once telemetry. Clear classifications do not bump workflow version.

Sandbox errors carry a frozen private diagnostic value through the existing
supervisor protocol and error wrappers. Finite stage/reason, elapsed time,
bounded errno/exit code and separate cleanup outcome explain the failure.
Optional numeric slot/cgroup snapshots are best effort. Public errors and
trusted not-executed/release/death/quarantine proofs remain authoritative;
diagnostics never change retry permission or isolation. No raw stderr capture.

Use the existing OpenTelemetry pipeline, event/outbox tables and unittest engine.
No provider calls, new dependencies or database migrations are required for
diagnostic delivery. Failures whose original evidence lacks a concrete cause
remain unresolved until an instrumented reproduction identifies that cause.
