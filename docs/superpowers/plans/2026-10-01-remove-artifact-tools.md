# Remove dedicated artifact tools

> Workers use `executing-plans`, `ponytail`, existing unittest/TDD and verification skills. Root owns normative spec/hash and saved commits. Parallel ownership below is explicit; no overlapping writers.

The user confirmed that there are no old named artifact files to preserve and
requested removal of the three tools and their application code/configuration.
Export/import and its proposed journal are cancelled. No external bucket,
MongoDB database, cluster or other deployed resource is deleted.

## Ownership and change

- Runtime worker: `core_agent/app.py`, `runtime.py`, `config.py`,
  `python_exec.py`, `response_files.py`, deletion of `artifact_service.py`,
  directly affected existing runtime/transfer/Python/observability/PostgreSQL
  tests and a focused removal regression. Trace callers before deletion.
- Config worker: `.env.example`, `docker-compose.yml`, `pyproject.toml`,
  `uv.lock`, README and `tests/test_compose_contract.py`. Remove only exclusive
  S3/Mongo/named storage settings and `pymongo`; keep transport size/storage,
  snapshots, offload and push. Use uv to regenerate the lock.
- Root: normative files/hash, `AGENTS.md`, integration/review, CI and commits.

## Required behavior

- Remove `core_artifact_save/load/list` catalog, schemas, handlers, constructor
  state, child inheritance, model instructions and named service adapters.
- Reject retired tool names (including dotted aliases) and legacy exclusive
  environment settings with actionable names-only upgrade diagnostics.
- Enterprise UI/A2A files continue through existing atomic workspace admission;
  the remaining legacy/test transport explicitly rejects unsupported file input
  rather than silently dropping bytes or recreating named storage.
- Preserve independent MIME mappings needed by response files and outputs.
- Replace obsolete named-service tests with removal/replacement assertions;
  preserve adjacent A2A binary/empty/atomic/integrity/IPC/tenant/snapshot checks.
- No export/import code, new persistence schema or data migration.

## Verification

Targeted existing tests first; then fresh PostgreSQL/Keycloak canonical
`uv run python -m unittest discover -s tests -v`, `uv run ruff check core_agent tests`,
UI typecheck/build and `uv build --no-sources` as applicable. Independent review
before root saves the logical removal stage; no merge/push/deployment.

## Verified removal slice

- [x] Removed the three model tools, named service, exclusive configuration and
  MongoDB dependency; independent transport/offload/snapshot paths remain.
- [x] Targeted runtime gate: 409 tests, 90.556 seconds, no skips; config gate:
  seven tests, no skips. Independent review found no blocking issue.
- [x] Canonical Python3.12/schema23 PostgreSQL/Keycloak gate: 1599 tests,
  220.519 seconds, exit0, three dedicated skips; frozen sync, migration and Ruff
  also exit0. Evidence: `.local-evidence/artifact-removal-ci-final-*`.
- [x] Wheel/sdist and ARM64 image built; ordinary CI image import, preinstalled
  skills, CLI and Python library smoke passed. Removed named module/dependency
  absent from wheel/image, UI and independent transport modules present.
- [x] Upgrade guidance drains old active Tasks, rejects obsolete frozen
  capabilities and preserves unknown already-dispatched intent without replay.

This closes CLEAN-01, not the full enterprise release. Combined acceptance and
final native/target Kubernetes gates remain tracked by the main plan.
