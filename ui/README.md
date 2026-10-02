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

The public Keycloak client needs the exact login redirect URI
`https://<agent-address>/ui/`, the exact logout redirect URI
`https://<agent-address>/ui/?logged_out=1`, and Web Origin
`https://<agent-address>`. Register both under Valid Redirect URIs, or configure
the logout URI separately under Valid Post Logout Redirect URIs. Before
upgrading a manually configured client that permits only `/ui/`, add the
logout URI: Keycloak rejects an unregistered query before ending SSO.
The official adapter performs standard code flow with PKCE S256. Tokens stay
in its memory; every private request refreshes first and attaches Authorization.
`/api/identity` must confirm owner access before private screens mount. Auth
failure unmounts private state. No localStorage/sessionStorage token handling,
third-party font/script resources, execution of model HTML, or secret
values in query parameters are introduced.
Logout builds the Keycloak URL before clearing tokens and ends SSO; it returns
to an explicit signed-out screen without automatic login, including after
reload. Only the Sign in button starts login again. Clearing a token on expiry
or access failure does not initiate SDK login. Accepted Tasks keep running.

References: [official adapter guide](https://www.keycloak.org/securing-apps/javascript-adapter)
and [pinned package/types](https://github.com/keycloak/keycloak-js/tree/26.2.2).

## Available screens

- All pages of shared chat metadata; latest Task snapshot, text submission,
  same-Task follow-up, explicit cancel and authenticated passive subscription.
  Reconnection fetches persisted Task first. Ambiguous sends retain one exact
  body/messageId for user-triggered retry; the UI never retries mutations itself.
  Titles derive from available original owner input or attachment names; owner
  rename persists company-wide with revision CAS. UUIDs live in the chat menu.
  Request status comes from canonical Task, interactions and tool results.
  New events preserve reading position; a button explicitly jumps to the latest.
- Inline HITL, owner questions and guardrail decisions with unchanged digest,
  deadline and server outcome. Scoped material preview and authenticated file
  download use stored review IDs only.
  Canonically completed tool approvals and guardrail cards disappear from the
  chat; persisted decisions remain available to owners. Owner question answers
  remain visible, and local expiry alone does not hide a pending request.
- Owner settings, tool mode/exemption/origin/revision and trusted peer registry.
  CAS conflicts require reading current state; credentials are write-only.
  Tool rules use a compact searchable/filterable list and explicit per-rule
  saves. The positive checks checkbox maps to `!guardrails_exempt`; execution
  mode remains independent. There is no technical mode or batch-save mechanism.
- Agent replies and persisted history render CommonMark/GFM with tables, lists
  and fenced code through react-markdown. Mermaid blocks load the local bundle
  on demand and display static SVG Blob images; invalid diagrams keep source.
  Raw HTML is skipped and Markdown images never load automatically. Mermaid
  uses strict security, disabled HTML labels and fixed text/edge limits. Its
  image context cannot execute scripts or access credentials; CSP allows blob
  only for images. Tool records and owner input remain plain text.
  Tool calls/results join by Task/call ID into action cards; successful sequences
  collapse, while errors and owner requests stay visible. Original owner text
  remains separate from system attachment instructions. File cards and the
  incoming/generated file panel use authenticated scoped downloads and escaped,
  bounded text previews; unsupported formats offer download. Workspace cleanup
  retains age filtering, manual selection and explicit confirmation.

The existing owner APIs also provide full chat history, file uploads,
workspace/result downloads and cron screens. The required native browser gate
uses actual Keycloak, PostgreSQL and a sandbox Pod for login/logout, approvals,
file publication/downloads, settings, schedules and workspace cleanup. See
`spec/implementation-status.md` for requirement-specific evidence and remaining
release gates. The native browser gate also covers persisted titles, action
correlation, approval consequences, policy filters, text previews, reading
position and mobile keyboard focus. Mocked authentication is not proof of
browser authorization.
