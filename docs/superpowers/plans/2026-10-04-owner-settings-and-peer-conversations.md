# Owner settings and peer conversations implementation plan

> **For agentic workers:** Use `subagent-driven-development` for the independent settings task; keep source changes in this permanent repository. Follow the existing unittest, PostgreSQL and native browser gates.

**Goal:** Deliver aligned forms and clean history, live outbound A2A conversation cards with a right panel, and owner configuration of prompt, MCP connections and provider models.

**Architecture:** Keep canonical workflows and immutable admission snapshots. Read peer messages through the existing bounded GetTask executor and durable scheduler. Company owners manage settings; external callers cannot access owner APIs. Existing tasks retain their admitted prompt, model and MCP configuration.

**Tech Stack:** React/TypeScript/Vite, Python with uv, existing PostgreSQL stores and A2A transport.

## Task 1: Finish the two UI regression fixes

- [x] Reproduce field geometry failure in `tests/owner_ui_files_browser.mjs` using the existing native image.
- [x] Set `align-content: start` on the shared label rule in `ui/src/styles.css`.
- [x] Hide only available empty service placeholders in `historyEntries` in `ui/src/ActionCard.tsx`; preserve review/outcome/file/status entries.
- [x] Remove the generic service placeholder text in `ui/src/History.tsx`.
- [x] Run `npm run typecheck`, `npm run build`, the native owner browser gate, spec quality/lock and Ruff. Commit these fixes independently.

## Task 2: Company-owned agent configuration

- [x] Trace `core_agent/app.py`, `runtime.py` admission/recovery, `interactions.py`, `owner_api.py`, `config.py` and existing MCP/provider adapters; inspect two existing PostgreSQL store/owner route patterns.
- [x] Extend `spec/agent-configuration.md` with owner-managed profile prompt, configured-provider model list and MCP connections; record immutable running-task behavior and no secrets in owner reads.
- [x] Add regression cases to existing configuration, owner API, runtime and PostgreSQL suites. Use provider/MCP transport fixtures; do not contact paid LLMs.
- [x] Add durable versioned configuration with CAS, validated bounded inputs and encrypted MCP credentials. Reuse existing connection/encryption primitives and fixed provider route. Do not accept provider credentials or provider URL through model selection.
- [x] Add controls in `ui/src/Settings.tsx`; maintain explicit per-section save, readable errors and current selection if provider discovery fails.
- [x] Prove admission uses edited configuration and recovery uses immutable configuration. Run targeted tests and real PostgreSQL suite.

## Task 3: Outbound A2A conversations

- [x] Trace `remote_agents.py`, `remote_operations.py`, both schedulers, owner history/file projection and `Chat.tsx`/`ActionCard.tsx`.
- [x] Specify bounded persisted public request/reply/file/status entries, stable identity/dedup, owner/company scope, confidentiality and restart compatibility. Missing peer history must not be invented.
- [x] Add tests for progressive replies, repeated history snapshots, final files, hidden private reasoning, isolation and lease/timeout/recovery behavior.
- [x] Persist validated/redacted public A2A content atomically with scheduler checkpoint changes. Preserve pinned peer revision and final deadline.
- [x] Add owner-only conversation listing/detail and short-lived observation interest. An open visible panel caps reads at 15 seconds; expired interest restores adaptive 10/30/300 timing. Reuse existing claim fencing and prohibit parallel duplicate dispatch.
- [x] Add compact task card and right panel, Markdown/files/statuses, 15-second active refresh, keyboard close/focus restoration and mobile layout. Preserve the main chat scroll position.
- [x] Extend the native browser gate with a controlled public peer conversation fixture; run protocol, PostgreSQL and browser gates.

## Task 4: Release

- [x] Update acceptance, release, implementation evidence, exact spec hashes and stable `AGENTS.md` facts after passing checks.
- [x] Obtain read-only spec/security/code review and resolve material findings.
- [x] Commit logical changes, merge into local main and deploy the checked amd64 image to both existing clusters under prior authorization.
- [x] Verify actual running image and public UI/readiness. Do not connect the agents or send production A2A tasks automatically.

Verification: full suite with real PostgreSQL and Keycloak passed (1843 tests, 3 dedicated environment skips); the separate native owner browser gate passed in 180.528 seconds. TypeScript/build, Ruff, spec quality/lock and the final amd64 image module smoke passed. Security/code reviews covered immutable settings, current-material visibility, bounded provenance, owner scope and fenced polling.

Deployment verified on both existing clusters: https://37.44.196.209/ui/ and https://37.44.197.46/ui/. Each has one ready updated agent replica; HTTPS UI/readiness returned 200 and deployed settings, peer, runtime and history sources match the checked repository. Both run `core-agent-enterprice.cr.cloud.ru/core-agent@sha256:006efbe4f07b81e067a0a9ab5aaf8469d74ea8dd376ecd0a3c2a8f19d38d7216`. The agents were not connected and no production A2A tasks were sent.
