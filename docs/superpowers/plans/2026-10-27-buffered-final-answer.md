# Buffered Final Answer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Remove `core_response_begin` and publish every recipient's final answer only after generation completes.

**Architecture:** Reuse ordinary model turns, canonical durable output and existing A2A lifecycle events. Remove public token preview and answer-phase execution while retaining strict legacy checkpoint validation, saved final responses and failed co-issued calls. Provider streaming and the separate emergency budget finalizer remain internal mechanisms.

**Tech Stack:** Existing Python runtime/unittest, A2A SDK, PostgreSQL, React/TypeScript; use `uv`, no new dependencies or test harness.

## 1. Contract and proof ownership

- [x] Update `spec/runtime.md`, `tools.md`, `kernel-instructions.md`, `a2a-protocol.md`, `public-contract.md`, `agent-configuration.md`, `acceptance.md`, `releases/v1.md` and `implementation-status.md`; update `AGENTS.md`.
- [x] Root refreshes `tests/test_spec_lock.py`, then runs `uv run python -m unittest tests.test_spec_quality tests.test_spec_lock -v` (exit 0).
- [x] Record the passing ordinary proof for EXT-AC-BUFFERED; ENT-AC-84 remains partial until the required native browser gate runs.

## 2. Runtime and saved tasks — root ownership

Files: `core_agent/runtime.py`, `kernel.py`, `tools.py`, `config.py`; existing runtime, kernel-recovery, interaction and PostgreSQL tests.

- [x] Remove the control definition/dispatch, answer-phase catalog/instruction branches and public delta publication. Keep the reserved name; unresolved calls use existing linked `TOOL_UNAVAILABLE` and current catalog.
- [x] Emit kernel snapshot v2 without phase guidance. Validate v1 exact shape/digest and legacy compiled text before adapting; retain pinned safety/host/base/capability texts.
- [x] Exercise legacy pending control, saved answer markers, saved final responses and blocked co-issued calls: no discarded output, replay, budget reset or revived tool. Preserve separate emergency finalization.
- [x] Run `uv run python -m unittest tests.test_runtime_observability tests.test_interactions tests.test_postgres_persistence -v` with the existing real PostgreSQL configuration (exit 0).

## 3. Transport and UI — independent workers

Files: `core_agent/app.py`, `a2a_sdk.py`, `streaming.py`; affected `ui/src/` and Storybook scenarios; existing `tests/test_auth.py`, `test_transfer_features.py`, `test_owner_ui_files_browser.py`.

- [x] Remove reply hub/preview transport and UI state. Preserve persisted-Task-first Subscribe, lifecycle status events, explicit Messages/Artifacts and canonical reconnect/polling.
- [x] Update existing blocked-provider tests to observe no partial answer before release and one final answer afterward, without additional model/tool calls. Owner, external and child paths share this behavior; stale preview frames remain invisible.
- [x] Run `uv run python -m unittest tests.test_auth tests.test_transfer_features -v` (exit 0); run `npm test`, `npm run typecheck` and `npm run build` in `ui/` and the affected Storybook checks.

## 4. Integration — root ownership

- [x] Run `git diff --check`, `uv run ruff check core_agent tests`, and `uv run python -m unittest discover -s tests -v` with the existing PostgreSQL/S3/Keycloak fixtures.
- [ ] Run the affected required native browser gate; absent Kubernetes fixtures leave this proof open.
- [x] Update status only from passing automated evidence and refresh spec hashes after the final normative edit.
- [x] Save the reviewed logical change in the permanent `.enterprise-work/cloudru-ai-agents` worktree. Publish only the Cloud.ru candidate under the previously authorized image workflow; target deployment remains separate.

## Verification

- Ordinary suite: `uv run python -m unittest discover -s tests -v`, exit 0; 2446 tests, 16 skips, 605.126 seconds (2026-10-27). Existing platform skips remain visible; no native/browser or target-rollout claim.
- Targeted transport/auth/real PostgreSQL: 121 tests passed. Legacy remote HTTP regression reproduced raw prefixes, then passed after their relay was removed; final result collection remains unchanged.
- UI: `npm test`, `npm run typecheck`, `npm run build` passed. Existing native browser fixture/assertions now require an invisible blocked-provider prefix and a single final Markdown answer.
- Independent review reproduced and fixed stale legacy phase dropping a new tool batch; follow-up/notification and saved-final cases are covered.
- Actual Cloud image smoke and all 74 source/64 UI file hashes matched; obsolete base UI assets removed. Recipe uses exact published batch4 base because the full Dockerfile's daemon registry mirror remains unavailable.
- First full run was interrupted for the confirmed legacy remote relay fix; the second completed against unchanged sources. Private build/test receipts are retained in the persistent worktree parent.
