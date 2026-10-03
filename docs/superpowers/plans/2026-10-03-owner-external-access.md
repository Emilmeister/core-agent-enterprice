# Owner external access implementation plan

**Goal:** Owners issue external-agent access through «Доступ к агенту», using their current Keycloak session, with a single token display.

**Architecture:** The owner API calls the configured realm's Admin REST API with the owner's bearer. Keycloak holds clients, service identities, issuance metadata and revocation state. No application credential registry, schema migration or new dependency is required.

**Accepted decisions:** 1–365 whole days, default 30; names up to 100 characters; separate identity for each newly admitted external agent; replacement preserves identity and invalidates earlier tokens. Existing manually configured external accounts remain visible. The owner's broad Keycloak administration rights are explicitly accepted by the user. Runtime/model tools cannot call this owner API.

## Implementation

- [x] Update AUTH-02, owner API contract, ENT-AC-85 and release scope; refresh the exact spec lock.
- [x] Add `core_agent/external_access.py`: bounded, nonredirecting, proxy-free Keycloak calls; validate inputs; list external clients with this API audience; native client creation and role/scope setup; guarded token issuance, replacement and disabling.
- [x] Connect owner-only routes through `core_agent/owner_api.py` and `core_agent/app.py`, reusing authenticated transport and no-store responses. Upstream errors expose only fixed application codes.
- [x] Extend the existing unittest suite with real owner HTTP requests and a controlled Keycloak HTTP transport: owner/external access, delegated bearer, duplicate creation, issuance ambiguity, stable identity, revocation, malformed input, response bounds and absence of credentials in listings/errors.
- [x] Add `ui/src/ExternalAccess.tsx`, reuse `Api` and native `dialog`, add navigation in `App.tsx`; expose count/name/status/expiry, accessible create/replace modal, one-time token copy and revoke. Clear token on close/unmount; preserve request ID for an uncertain create result.
- [x] Extend real isolated Keycloak integration and browser checks using existing fixtures rather than introducing a testing framework.
- [x] Document required owner permissions, Keycloak-held metadata and new endpoints in AGENTS/README; no model catalog change.

## Verification

Run `uv run ruff check core_agent tests`, targeted unittest modules and `uv run python -m unittest discover -s tests -q`. Run `npm run typecheck` and `npm run build` in `ui/`, then `uv build --no-sources`. Isolated Keycloak integration must prove actual requested expiry, external-only token and rejection after replacement/revocation. Save the logical change as a reviewed commit in this permanent branch.

Deployment bootstrap must assign owner administration roles and expose them in browser client scope; no additional provisioning credential is introduced. Update the separate deployment repository within the already authorized deployment workflow and run its existing offline gates. Live rollout follows successful verification, without automatically linking the two agents or sending A2A tasks.

Verification passed: canonical suite on Python 3.12 with fresh local PostgreSQL and real Keycloak; required native Pod/Chromium gate; UI typecheck/build, lint, package/image smoke and deployment offline gates. Live rollout uses the immutable amd64 image after the reviewed commit.
