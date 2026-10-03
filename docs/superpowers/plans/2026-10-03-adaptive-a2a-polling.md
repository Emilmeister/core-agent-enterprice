# Adaptive A2A polling implementation plan

**Goal:** detect short remote tasks sooner while retaining bounded polling for long waits.

**Architecture:** keep the existing remote scheduler, immutable contract and checkpoint.
Compute dispatch time as `deadline - timeout_seconds`; use the authoritative clock
after each network step. Schedule at most 10 seconds before age180, at most30
before age780, then the pinned interval. Cap all scheduling at the final deadline
and the next phase boundary. Existing short custom intervals remain effective.
Persisted schedules survive deployment; new cadence applies when rescheduling.

**Tech stack:** Python standard library, existing unittest and PostgreSQL fixtures.

- [ ] Update normative target, API/settings descriptions, acceptance and release scope.
- [ ] Add behavior tests in `tests/test_remote_operations.py`: due-time and phase
  transitions, delayed retry, short configured intervals, terminal deadline, and
  restart in the middle/late phases through a new real PostgreSQL pool.
- [ ] Run the new in-memory tests with `uv run python -m unittest` and confirm
  failure on the old fixed interval.
- [ ] Change only `RemoteA2AExecutor._poll_later` in
  `core_agent/remote_operations.py`; update `ui/src/Settings.tsx` to explain the
  configured interval's late-phase scope; update stable guidance in `AGENTS.md`.
- [ ] Run the targeted real PostgreSQL suites, Ruff, spec quality/lock and UI
  typecheck/build; run the full canonical unittest suite with PostgreSQL/Keycloak.
- [ ] Record CI proof in implementation status and exact spec hash lock; review
  the complete diff, save one validated commit and merge into local main.
- [ ] Publish an immutable image and deploy to both existing clusters; verify
  running digest and readiness. Do not create external tasks or connect agents.
