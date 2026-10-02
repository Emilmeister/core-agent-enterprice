# Owner SPA

React/TypeScript/Vite source for the existing Core Agent service. This is a
foundation, not a completed enterprise UI release.

## Build and dependency evidence

Dependencies use exact versions in package.json and the checked-in
package-lock.json. Use Node 22.12 or newer and install from the lockfile:

```sh
cd ui
npm ci
npm run typecheck
npm run build
```

The production build writes generated files to `core_agent/ui_dist/`, which
the existing ASGI service serves and `uv build --no-sources` includes in the
Python package. Run the frontend build before building the Python package.
CI installs the lockfile, checks TypeScript and builds these assets. Docker
builds them in a separate Node 24 stage and copies only the compiled assets
into the Python image. No credential belongs in a Vite environment variable
or build output.

## Service integration

The service serves `core_agent/ui_dist/index.html` at `/ui/` and its assets at
`/ui/assets/` when the public browser client is configured. The existing
`/ui/config` route returns only issuer and public client ID. Static routes apply
CSP and cache/security headers: scripts and styles stay on the same origin,
connections allow only the same origin and configured issuer, and missing
API/A2A paths never receive the SPA shell. Run development through a same-origin
reverse proxy to the existing ASGI service; the Vite dev server alone does not
implement its APIs.

The public Keycloak client needs exact `/ui/` redirect URI and Web Origin.
The official adapter performs standard code flow with PKCE S256. Tokens stay
in its memory; every private request refreshes first and attaches Authorization.
`/api/identity` must confirm owner access before private screens mount. Auth
failure unmounts private state. No localStorage/sessionStorage token handling,
third-party font/script resources, HTML rendering of model content, or secret
values in query parameters are introduced.

References: [official adapter guide](https://www.keycloak.org/securing-apps/javascript-adapter)
and [pinned package/types](https://github.com/keycloak/keycloak-js/tree/26.2.2).

## Available screens

- All pages of shared chat metadata; latest Task snapshot, text submission,
  same-Task follow-up, explicit cancel and authenticated passive subscription.
  Reconnection fetches persisted Task first. Ambiguous sends retain one exact
  body/messageId for user-triggered retry; the UI never retries mutations itself.
- Inline HITL, owner questions and guardrail decisions with unchanged digest,
  deadline and server outcome. Scoped material preview and authenticated file
  download use stored review IDs only.
- Owner settings, tool mode/exemption/origin/revision and trusted peer registry.
  CAS conflicts require reading current state; credentials are write-only.

Full chat history, file upload/workspace/result downloads and cron screens are
not presented as working. No endpoint for them is invented here. Chat labels
are provisional because the current sidebar endpoint returns identifiers only.

Still required: actual browser mobile, keyboard and malicious-content checks;
real Keycloak login/refresh/logout;
two-owner races, stream reconnect and durable backend integration. No mocked
authentication result counts as proof of browser authorization.
