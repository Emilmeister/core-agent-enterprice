# Next steps owner settings implementation plan

**Goal:** Configure optional planning and its period through the existing owner UI.

**Architecture:** Add a nullable `next_steps` settings override containing exactly
`enabled: bool` and `every: positive int`. Reuse company revision/CAS storage and
root admission snapshots. ENV supplies defaults; explicit deployment allowlists
still limit capabilities. Running tasks and recovery retain their admitted period.

**Stack:** Existing Python runtime/unittest, PostgreSQL JSONB, React/TypeScript.

- [x] Extend normative NEXT-01 configuration/acceptance/release and guidance.
- [x] Add failing settings/runtime regression checks in existing unittest suite:
  owner-only save/reset, invalid types and CAS, disabled ENV enable, deployment
  ceiling, old root/child/recovery pinning, real PostgreSQL reopen.
- [x] Implement settings projection/validation and root-only application, retaining
  registered capability independently of deployment activation defaults.
- [x] Add a native switch/number field/save/reset section to Settings.tsx, effective
  values and inheritance types; extend existing Storybook mocks and checks.
- [x] Run targeted Python checks, frontend typecheck/test/build and a local browser
  save/reset/conflict check. Run the ordinary CI suite with owned local fixtures.
- [x] Update implementation proof and frozen hashes, review diff, commit one change.

User explicitly authorized related spec/tests/hash changes in this turn. No new
test framework, SQL migration, provider call, deployment or telemetry change.

Validation: 62 targeted checks with real PostgreSQL passed; ordinary suite ran
2482 tests with 16 platform-dependent skips and no failures. UI typecheck/test/build,
Storybook typecheck and 10 demo tests passed. Cached Chromium executed four planning
stories at 390px without page/console errors or form overflow. Browser demos use an
in-memory API; durable settings and recovery are proven by real PostgreSQL tests.
Ruff and Python wheel/sdist build passed. Native sandbox/browser platform gates and
the live Cloud.ru rollout were not run for this change.
