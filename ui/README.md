# Owner SPA

React/TypeScript/Vite owner interface served by the existing agent backend.
Release readiness is tracked in `spec/implementation-status.md`.

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
  Initial reads and SSE-only failures with healthy canonical polling stay quiet;
  an actual canonical read outage displays the recovery notice until restored.
  Confirmed deletion archives an idle chat for all company owners and disables
  its schedules. Active work must finish or stop first. Existing A2A results,
  history and issued files remain available through authenticated scoped APIs.
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
- Company agent settings edit the system profile and select a model from the
  configured provider's inventory. MCP connections can be added by URL with a
  custom authorization header, enabled, disabled or deleted; each connection
  saves separately. Credentials are write-only. New tasks use the saved revision;
  admitted tasks, children and recovery keep their pinned configuration.
- Outbound public A2A conversations open beside the main chat. The selector
  chooses a counterparty by stable connection identity; all operations to that
  peer in the current chat share one chronological message/file/status history.
  Other peers have separate histories; new operations preserve the selection.
  On desktop, drag the separator or use arrows/Home/End to resize within bounds
  that preserve the main chat. Width stays within the open chat; mobile uses a
  full-width overlay without a resize handle.
  Recreating a connection with the same name does not merge its old history.
  Visible active operations update every 15 seconds, with expiring observation
  interest; closing the panel restores adaptive backend polling. Main chat and
  panel scroll independently. Private reasoning and owner exchanges are excluded.
- Agent access lists native Keycloak service accounts. Owners issue, replace,
  revoke or permanently delete credentials using their own authorized session.
  Issued tokens appear once and are cleared when the dialog closes. Permanent
  deletion removes the identity, while existing task/file data remains available
  to owners. This screen requires the documented realm-management permissions.
- Live public replies arrive as scoped cumulative A2A snapshots and render as
  provisional plain text until canonical history supplies the final answer.
  Partial frames do not fetch history individually. Live text is enabled only
  for a message accepted in the current open chat; reopening the page reads
  canonical history/status without restoring an earlier preview. Temporary
  disconnection within that open chat may restore its process-local preview
  without starting work; a server restart may discard
  this transient preview while the durable Task continues. Reasoning and private
  owner exchanges do not enter this public stream.
  Persisted replies render CommonMark/GFM with tables, lists
  and fenced code through react-markdown. Mermaid blocks load the local bundle
  on demand and display static SVG Blob images; invalid diagrams keep source.
  Raw HTML is skipped and Markdown images never load automatically. Mermaid
  uses strict security, disabled HTML labels and fixed text/edge limits. Its
  image context cannot execute scripts or access credentials; CSP allows blob
  only for images. Tool records and owner input remain plain text.
  Tool calls/results join by Task/call ID into compact clickable action cards.
  A native modal shows arguments, readable output and technical data, with
  keyboard opening, Escape closing, restored focus and bounded scrolling.
  Successful sequences
  collapse, while errors and owner requests stay visible. Original owner text
  remains separate from system attachment instructions. File cards and the
  incoming/generated file panel use authenticated scoped downloads and escaped,
  bounded text previews; Markdown previews use the same safe reply renderer.
  Python source uses lowlight and the pinned highlight.js grammar, rendered as
  React text/spans. Indentation remains intact and large code uses plain text.
  Expanded action groups and file previews scroll within bounded regions with
  keyboard access, keeping the answer and file properties reachable.
  Unsupported formats offer download. Workspace cleanup
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
