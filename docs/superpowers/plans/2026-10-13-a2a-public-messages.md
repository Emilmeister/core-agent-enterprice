# A2A Messages and Explicit Artifacts Implementation Plan

> **For agentic workers:** Use subagent-driven-development for the independent file groups below. All edits belong to the permanent worktree `.enterprise-work/a2a-public-messages`.

**Goal:** Publish replies as durable Messages and attach typed immutable Artifacts immediately only after an explicit model tool call.

**Architecture:** Pin output contract v2 in each new workflow snapshot. Reuse scoped response-file snapshots for all Artifact Parts. Commit public output with the fenced workflow transition and canonical Task projection; streams deliver the committed output without becoming its authority. Missing version retains the legacy contract.

**Tech Stack:** Python, existing unittest suite, official A2A SDK, PostgreSQL/in-memory workflow stores, React/TypeScript. No additional service or test framework.

## 1. Normative contract

- [x] Update the related normative documents, acceptance criteria and release scope; refresh the exact spec hash lock.
- [x] Confirm `core_response_artifacts` appends an immutable batch immediately, ordinary replies are Messages, and old accepted runs are not rewritten.
- [x] Run `uv run --no-sync python -m unittest tests.test_spec_quality tests.test_spec_lock -v` before implementation.

## 2. Typed snapshots

Files: `core_agent/response_artifacts.py`, `core_agent/response_files.py`, `tests/test_response_artifacts.py`.

```python
ResponseArtifactService.prepare(binding, artifacts, *, task_id, run_id,
    publication_id, limit_bytes, published=())
ResponseArtifactService.load(binding, manifests, *, task_id, run_id,
    limit_bytes, connection=None)
ResponseArtifactService.receipts(manifests)
```

- [x] Write and run failing tests for mixed text/data/file Parts, immutable file capture, invalid/foreign refs, whole-batch limits and deterministic replay IDs.
- [x] Implement closed typed inputs, exact UTF-8/canonical JSON snapshots, bounded manifests and task-wide pinned limits through existing storage.
- [x] Run `uv run --no-sync python -m unittest tests.test_response_artifacts tests.test_response_files -v`.

## 3. Runtime publication

Files: `core_agent/app.py`, `core_agent/runtime.py`, `core_agent/tools.py`, `core_agent/config.py`, `core_agent/kernel.py`, `core_agent/workflow.py`, `core_agent/streaming.py`, `tests/test_public_output_runtime.py`.

- [x] Write failing composition-root tests: normal final reply has no implicit Artifact; attach completes while the Task continues; a second attachment appends; denied/HITL calls do not publish; working Messages do not end the Task.
- [x] Pin snapshot fields `output_contract_version=2`, `published_artifacts=[]`, `public_messages=[]` at admission. Add `core_response_artifacts` and `core_message_send` with existing schema/policy/guardrail/budget paths.
- [x] Commit prepared output only with the successful fenced tool result. Invoke canonical Task reconciliation in the same store transaction; publish committed stream updates afterward.
- [x] Retain v1 file replacement/clear behavior. V2 `core_response_files` is an immediate file-only compatibility alias; an empty list does not retract published output.
- [x] Run `uv run --no-sync python -m unittest tests.test_public_output_runtime tests.test_interactions tests.test_runtime_guardrails -v`.

## 4. Canonical A2A projection

Files: `core_agent/a2a.py`, `core_agent/a2a_sdk.py`, `core_agent/database.py`, `tests/test_public_output_projection.py`, existing A2A tests.

- [x] Write failing tests for working and terminal Messages/Artifacts, stable identity, Get/history limits, restart and concurrent Get/publication.
- [x] Add compatible optional Part media type and Artifact name fields; introduce `PublishedOutput` and `workflow_public_output` for v2 while retaining legacy Artifact results.
- [x] Use the same projection in live execution, persisted Get, recovery and push. Preserve completed explicit Artifacts on failure/cancel. Avoid Task/run lock inversion.
- [x] Mark complete immutable Artifacts with `core_agent_artifact: {version: 1, complete: true, immutable: true}`. Never convert ordinary final text to an Artifact.
- [x] Run `uv run --no-sync python -m unittest tests.test_public_output_projection tests.test_a2a_sdk -v` and relevant real PostgreSQL tests.

## 5. Receiving side and UI

Files: `core_agent/remote_agents.py`, `core_agent/remote_artifacts.py`, `core_agent/remote_operations.py`, related scheduler/file/history modules and tests; `ui/src` where required.

- [x] Verify ordinary Messages stay inline and completed marked Artifacts can enter quarantine/review before the remote Task ends.
- [x] Preserve stable receipts and paths across repeated snapshots; publish accepted full Parts only. Partial/unmarked third-party Artifacts keep their terminal fallback.
- [x] Show replies as conversation and selected Artifacts as separate results, including text/data/file and unchanged old results.
- [x] Run targeted remote/files/browser tests; run UI typecheck/build for UI changes.

## 6. Integration

- [x] Run canonical Ruff and complete unittest gates; run the affected real PostgreSQL and Compose/native sandbox checks.
- [x] Update implementation status only for proof that enters the normal CI gate; update AGENTS/README for actual changes.
- [x] Save reviewed logical commits, merge the feature branch into `main`, and rebuild the local Compose image with the existing private configuration.

Local rollout verification: the running Compose agent uses the tested native arm64 image; readiness, UI and owner authentication passed. A real provider response completed as a stable final Message without implicit Artifacts. The initial guardrail classifier reached its configured 60-second timeout; the known test input was approved through the owner API before execution resumed. Security settings and existing volumes were preserved.
