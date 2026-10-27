# Cloud.ru next steps implementation plan

> **For agentic workers:** Use subagent-driven-development with disjoint file ownership. Preserve Cloud.ru contracts and validate before committing.

**Goal:** Port only optional `core_next_steps` from main, including ordinary HITL/policy enforcement and persisted cadence.

**Architecture:** Reuse the existing built-in schema/dispatch path, normal transcript and workflow snapshot. Use request-local tool choice only when planning is due. No timeout/backoff, provider-body logging, response-control tool, transport, database-schema or dependency change.

**Tech Stack:** Existing Python/uv, unittest, PostgreSQL fixtures, OpenAI-compatible and Anthropic adapters.

## Approved scope

User approved only comparison item 1 and explicitly allowed related spec/tests/hash lock changes. NEXT-01 in normative tools/runtime/configuration/acceptance is authoritative. Main is `3a88529`; Cloud starting point is `547cb98`.

## Tasks

- [x] Define NEXT-01: default disabled, period 5, root-only advisory plan,
  first turn/owner or wait outcomes/inbound input/N calls, two failures release,
  ordinary policy/HITL/budgets, persisted settings/counters and legacy disabled.
- [x] Model worker: add main's planning wire regression cases to
  `tests/test_transfer_features.py`, run RED, port only `tool_choice` in
  `core_agent/model.py`, rerun GREEN. Keep Cloud summary/cap/error protections.
- [x] App worker: port schema and validated env switches in app/config; add
  meaningful config regression tests before implementation; wire Compose and
  `.env.example`, document usage in Cloud instructions and AGENTS.
- [x] Runtime worker: port main runtime tests into `tests/test_next_steps.py`,
  run RED, add ordinary read-only handler and snapshot cadence to runtime.
  Check real HITL/live deny, legacy/restart, compaction and child/finalizer behavior.
- [x] Integrate and independently review: only planning changes; reserved name,
  current catalog and old capabilities do not grant new tools during recovery.
- [x] Run targeted tests, `uv run ruff check core_agent tests`, actual PostgreSQL
  checks and `uv run python -m unittest discover -s tests -v` using existing local
  fixtures. Run `docker compose config --quiet` for environment wiring.
- [x] Update implementation traceability to reflect actual proof, refresh exact
  spec hashes, run `uv run python -m unittest tests.test_spec_quality tests.test_spec_lock -v`
  and `git diff --check`, then save one logical validated commit.

## Compatibility

No SQL migration. New root snapshots pin the optional cadence; children and
legacy snapshots without the field stay disabled. Counters are committed with
tool outcomes outside compacted transcript. Current tool permissions may narrow
access, never expand the original admission ceiling. User final text is unchanged.

## Validation notes

Use native project unittest proofs; do not add a second BDD harness. Local suite
does not establish target Cloud Pod/native sandbox/browser gates. No publication
or deployment is part of this feature port unless separately authorized.

## Verification outcome

Full ordinary suite on final production code: 2476 tests, 16 platform-dependent
skips, exit 0 in 438.606 seconds with local PostgreSQL/MinIO/Keycloak. New
planning, policy and PostgreSQL tests passed. First full run was interrupted
after review found missing nested-call cadence; the shared completion points
were fixed before the successful run. The lifted-wait test was strengthened
during that run without changing production code, then rechecked separately.
Ruff, Compose configuration, spec quality/hash lock and diff checks passed.
Native sandbox/browser/target Cloud proof is not claimed.
