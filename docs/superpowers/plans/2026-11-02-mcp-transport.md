# Per-MCP transport and complete discovery implementation plan

**Goal:** Read the complete MCP catalog and support optional per-connection proxies and mTLS with a shared image-owned trust store.

**Architecture:** Extend the existing owner configuration, encrypted immutable settings revisions and Streamable HTTP connector. Keep credentials out of admission snapshots; hydrate them from the pinned revision for root, child and recovery. Use existing HTTPX, stdlib SSL and cryptography; keep the repository's standard-library unittest runner.

**Approved behavior:** No automatic Authorization prefix. An absent proxy means direct connection, independent of process proxy variables. Client identity is optional and belongs to one MCP. CA trust is common to all MCP, packaged in the image, with TLS verification always enabled. No vendor-specific protocol branch.

## Contract and file ownership

- Lead: normative spec/acceptance/release edits and hash lock only after explicit frozen-file authorization; UI/API integration, common CA packaging, AGENTS and final validation.
- DSH transport author: `core_agent/mcp.py`, new `core_agent/mcp_transport.py`, `tests/test_mcp_transport.py` after authorization.
- DSH settings author: `core_agent/agent_settings.py`, runtime credential hydration in `core_agent/runtime.py`, `tests/test_mcp_connection_settings.py` after authorization. Separate permanent worktree for parallel writers.
- Fresh DSH reviewer: read-only final stable diff, transport isolation, secret persistence, compatibility and evidence.

## API shape

Existing connection fields remain name/url/enabled/header_name/header_value. Add:

- `proxy_url`: optional HTTP/HTTPS URL without userinfo, query or fragment. Omitted preserves an existing value; null clears proxy and its credentials. New connections default to direct.
- `proxy_username` and `proxy_password`: write-only pair. Both omitted preserve; both null clear; both strings replace. A changed proxy URL cannot silently inherit prior proxy credentials.
- `client_certificate`: write-only nullable upload. PKCS12 uses `{format: "pkcs12", data: "base64", password: "..."}`. PEM uses `{format: "pem", certificate: "PEM chain", private_key: "PEM key", password: "..."}`. Omitted preserves; null clears. Password may be omitted for unencrypted input.
- Read projection adds proxy_url, has_proxy_credentials, has_client_certificate and certificate_info (subject, issuer, expiry, SHA256). No secret values or input file paths.
- Changing destination URL requires explicit replacement/removal of retained mTLS identity, like the current auth-header rule. Secret-bearing fields are not accepted from model input.
- Certificate inputs are bounded and parsed on save; verify key/cert match and validity. Normalize certificate chain and key into encrypted revision storage; discard upload passwords. Retain compatibility when reading version1 header-only envelopes.
- Use the same shared trust roots for destination and HTTPS proxy but separate SSL contexts: never present the destination client identity to the proxy.

## Work sequence

- [x] Baseline existing owner-settings tests and spec lock in isolated Python3.12 environment: 16 tests passed, no skips (`uv run --offline --no-sync python -m unittest tests.test_interactions.OwnerAgentConfigurationAPITests tests.test_spec_lock -q`).
- [x] User explicitly authorized related spec/tests/hash changes on 2026-11-02.
- [x] Update `spec/agent-configuration.md`, `spec/tools.md`, relevant acceptance and release scope; leave implementation status pending until evidence passes.
- [x] Transport author writes regression cases and observes failures before implementation: all pages included; repeated/malformed cursor, duplicate names, page/error/deadline failures never return a partial catalog; literal auth headers; no environment proxy; isolated TLS contexts.
- [x] Settings author writes failures then implements encrypted normalization, write-only projections, preserve/clear/replace, URL binding, legacy envelope compatibility and pinned root/child/recovery credentials.
- [x] Integrate authors into the permanent worktree, then wire UI uploads/proxy controls. Keep each connection's unsaved fields isolated and clear secret drafts after successful save.
- [x] Package public `sber_ca.pem` as a generic shared root certificate. Source is `/Users/ermadatov/Documents/Projects/sber-mcp/sber_ca.pem`, subject Russian Trusted Root CA, SHA256 d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31, CA=true, valid until 2032-02-27. Never copy `.env`, PKCS12, client key or tokens into the repository or image.
- [x] Local real-network fixtures prove HTTP CONNECT and HTTPS proxy, mTLS identity separation, direct connection and invalid trust/auth failures. Synthetic certificates only. Check full catalog with real JSON/SSE exchanges.
- [x] Run targeted tests, settings/runtime regressions, full ordinary suite with available isolated fixtures, Ruff, UI typecheck/build and package/image checks. Report platform-only gates separately.
- [x] Fresh DSH review; correct substantive findings and re-run affected checks.
- [x] Update implementation evidence and frozen hashes, check AGENTS facts, commit one reviewable product change. No publish/deployment or real Sber tool calls in this task.

## Verification commands

```sh
uv run python -m unittest tests.test_mcp_transport tests.test_mcp_connection_settings -v
uv run python -m unittest tests.test_interactions.OwnerAgentConfigurationAPITests tests.test_runtime_observability tests.test_spec_quality tests.test_spec_lock -q
uv run ruff check core_agent tests
uv run python -m unittest discover -s tests -v
cd ui
npm ci
npm run typecheck
npm run build
```

Tests use existing unittest discovery so they enter ordinary CI. Do not add a parallel test framework. Frozen-file authorization and provider availability are operational constraints, not grounds to weaken acceptance.

## Original implementation preparation

Permanent worktree: `.enterprise-work/mcp-transport`, branch `feat/mcp-transport`, base `6a0be33`. Settings author uses separate permanent `.enterprise-work/mcp-settings` worktree.

DSH control with inherited proxy environment failed; the same configured `cloudru/qwen3-8-flash-next2` with `xhigh` returned `DSH_READY` in 2.4 seconds when proxy variables were removed for that process. Workers use that direct process environment; desktop provider/model and global configuration remain unchanged.

## Original feature review and local evidence (4952f56)

Two authors and fresh transport/settings reviewers were followed by a final
independent DSH review. Lead reproduced and fixed the findings: encrypted PEM
password encoding, inherited transport hydration, incomplete envelopes, empty
proxy passwords, uppercase HTTPS schemes, invalid Unicode cursors and credentials
without a real proxy. The final UI finding was corrected: a login edit cannot
silently replace a saved password with an empty value.

Local Chromium exercised the real Settings component with an in-memory API:
incomplete proxy pair blocked, explicit empty password accepted, another MCP's
draft preserved, PKCS12 upload bytes/password sent and cleared, certificate
removal sent explicitly. This is UI smoke evidence, not the dedicated native
Pod/browser gate. No live Sber server was contacted.

The 28 transport tests also passed inside the non-root local Docker image
`core-agent:mcp-transport-local`. Wheel/sdist include the common CA and owner UI.
TLS test material uses a unique temporary directory per process so parallel
review/test invocations cannot overwrite each other's synthetic CA.

Final ordinary suite: 2251 tests, exit 0, four skips, with disposable PostgreSQL
(including pg_dump/restore) and Keycloak. The skips are the intentionally
memory-only loop-ownership case (PostgreSQL leader is covered separately), the
dedicated Docker restart fixture, native Pod/browser fixture and host Linux
sandbox fixture. Those native gates were not claimed as verified by this change.
Ruff, spec quality/hash lock, owner UI typecheck/build, `uv build --no-sources`,
Docker build and the non-root image smoke passed. Image MCP tests: 28 passed.

## Integration into the supported branch

Target branch: `main`, baseline `3a88529`. Only the MCP product change from
`4952f56` is ported; the adaptation-plan branch is not merged. Existing branch
runtime and deployment behavior is retained. Historical evidence above belongs
to the original feature branch; fresh branch verification is recorded below.

Fresh main verification (2026-11-02): `uv run python -m unittest discover -s tests -v`
completed with exit 0: 2274 tests, four documented fixture skips. Disposable
PostgreSQL (including dump/restore) and Keycloak were enabled; skipped cases are
memory-only loop ownership, native Docker restart, dedicated Pod/browser and host
Linux sandbox. Native production gates are not claimed. Targeted MCP/settings/spec
checks, Ruff, UI typecheck/build and `uv build --no-sources` passed. Wheel inspection
confirmed packaged CA and UI. Independent DSH static review found no demonstrated
criterion violations; behavioral checks were executed by the lead.
