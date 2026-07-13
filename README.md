# Core Agent

Policy-enforced Python agent runtime implemented from the immutable [product specification](spec/README.md).
Its task input contains only `prompt`, `mcp`, and `skills`; platform policy, kernel behavior,
approvals, isolation, durability, context management, tasks, and telemetry remain inside the runtime.

## Development

```bash
uv sync
uv run python -m unittest discover -s tests -v
uv run python -m unittest tests.test_end_to_end -v
uvx ruff check core_agent memory_service
uv build --no-sources
```

The end-to-end suite runs without external credentials. It crosses the official A2A HTTP binding,
an OpenAI-compatible model server, real local PTYs, background tasks, focused child agents,
Streamable HTTP Memory MCP, Markdown indexing/NER/graph search, and explicit skill activation.

## Run the agent

### Runtime prompt

The replaceable role/profile prompt is `CORE_AGENT_PROFILE`; local Compose reads it from `.env`.
If it is absent, the exact default is `Complete the user's task using available tools.`. This is
only the `AgentProfilePrompt`: the effective model instructions are compiled for every run from
the protected safety, host-policy, kernel and enabled-capability layers, followed by this profile
and selected skills. The default and protected layers are assembled in
[`core_agent/app.py`](core_agent/app.py), compiled in [`core_agent/runtime.py`](core_agent/runtime.py),
and specified in [`spec/kernel-instructions.md`](spec/kernel-instructions.md). The protected layers
cannot be replaced through `.env`, A2A input, MCP, memory, skills, or tool output.

Production state requires PostgreSQL. Apply the versioned schema before starting the app:

```bash
export DATABASE_URL='postgresql://core_agent:password@database:5432/core_agent'
DATABASE_MIGRATION_URL='postgresql://migrator:password@database:5432/core_agent' \
DATABASE_APP_ROLE=core_agent \
uv run core-agent-db migrate
CORE_AGENT_ENVIRONMENT=production \
CORE_AGENT_STATE_BACKEND=postgres \
DATABASE_AUTO_MIGRATE=false \
OPERATOR_JWT_HS256_SECRET='independent-32-byte-minimum-secret' \
OPERATOR_JWT_ISSUER='https://operator.example' \
OPERATOR_JWT_AUDIENCE='core-agent-operator' \
LOCAL_APPROVAL_EXTENSION_URI='https://agent.example/a2a/extensions/local-operator-approval/v1' \
PUSH_NOTIFICATION_ENCRYPTION_KEY='replace-with-generated-fernet-key' \
DURABLE_STORAGE_ROOT='/mounted-s3/core-agent' \
LOCAL_WORKSPACE_ROOT='/tmp/core-agent/runs' \
uv run core-agent
```

`DATABASE_URL` must come from the deployment secret store. Production startup fails closed when
the credential is missing, PostgreSQL is unavailable, or the schema version differs. A bounded
pool is configured with `DATABASE_POOL_MIN`, `DATABASE_POOL_MAX`, and
`DATABASE_CONNECT_TIMEOUT_SECONDS`; there is no production fallback to process memory or SQLite.
A2A tasks, workflow events, checkpoints, approval reservations, and append-only audit records all
use that pool. `/health/live` checks the process and `/health/ready` checks PostgreSQL and schema
readiness.
The migration job may use the separate `DATABASE_MIGRATION_URL`; with `DATABASE_APP_ROLE` it grants
that runtime role only the table-specific DML privileges it needs. Production rejects in-process
auto-migration so the serving credential does not require DDL rights.
The built-in private operator API verifies a separately-audienced HS256 JWT with `sub`, `jti`,
`exp`, and the `agent_operator` role. Its approve/deny endpoints require `If-Match` and the exact
action digest; A2A callers cannot use their credentials on this route.
`PUSH_NOTIFICATION_ENCRYPTION_KEY` is a separate Fernet key used to encrypt durable A2A webhook
configuration. Generate it with
`uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`.
`DURABLE_STORAGE_ROOT` must be the S3-backed mount used only for immutable snapshots and artifacts;
`LOCAL_WORKSPACE_ROOT` must be a separate local ephemeral path used by active processes.
`CORE_AGENT_ALLOWED_BUILTIN_TOOLS` can remove individual terminal, task, delegation, or artifact
tools; the resulting allowlist is reflected in both the Agent Card and every model tool catalog.
`CORE_AGENT_MAX_DEPTH` may lower delegation depth to `0` or `1`; `2` is the hard maximum, allowing
main → child → grandchild while rejecting any further delegation.
Final A2A results and `core.artifact.put` content are stored as tenant-scoped, digest-verified blobs
with PostgreSQL metadata. `ARTIFACT_MAX_BYTES` bounds each stored artifact.

OpenAI-compatible API (OpenAI, vLLM, Ollama, LM Studio, OpenRouter, or another compatible gateway):

```bash
MODEL_API_FORMAT=openai \
MODEL_BASE_URL=https://your-provider.example/v1 \
MODEL_NAME=your-model \
MODEL_API_KEY=your-key \
uv run core-agent
```

Anthropic Messages API:

```bash
MODEL_API_FORMAT=anthropic \
MODEL_BASE_URL=https://api.anthropic.com/v1 \
MODEL_NAME=your-model \
MODEL_API_KEY=your-key \
uv run core-agent
```

Production Memory MCP uses its own process and storage mount; Core only receives its MCP
descriptor. Development can use the deterministic local adapters with `uv run core-agent-memory`.
Production requires real embedding and NER providers:

```bash
MEMORY_ENVIRONMENT=production \
MEMORY_ROOT=/mounted-s3/memory \
MEMORY_ALLOWED_NAMESPACE_PREFIXES=session/ \
MEMORY_EMBEDDING_ENDPOINT=https://embedding.example/v1/embeddings \
MEMORY_EMBEDDING_MODEL=your-embedding-model \
MEMORY_EMBEDDING_API_KEY=your-embedding-key \
MEMORY_NER_ENDPOINT=https://ner.example/v1/extract \
MEMORY_NER_MODEL=your-ner-model \
MEMORY_NER_API_KEY=your-ner-key \
uv run core-agent-memory
```

`MODEL_ENDPOINT` overrides the complete request URL. Providers with custom authentication can use
`MODEL_HEADERS_JSON`; optional provider parameters belong in `MODEL_EXTRA_BODY_JSON`. An API key is
not required for a local OpenAI-compatible server:

```bash
MODEL_API_FORMAT=openai \
MODEL_BASE_URL=http://localhost:11434/v1 \
MODEL_NAME=your-local-model \
uv run core-agent
```

The A2A Agent Card is then available at `http://localhost:8000/.well-known/agent-card.json`.
Terminal execution is trusted by default for the single-container deployment; set
`CORE_AGENT_TRUST_TERMINAL=0` to require local-operator approval instead of automatic execution.
The explicit test/development state profile may use SQLite/in-memory, and the development operator
profile uses an in-process approve-all control-plane stub; the remote A2A caller
cannot approve or deny. Local wait is exposed as A2A `WORKING`, while the private approval records
and single-use execution reservations use the selected state backend.

`LOCAL_APPROVAL_DB_PATH` only configures the non-production SQLite test adapter.
`LOCAL_APPROVAL_EXTENSION_URI` configures the optional informational A2A extension.
`LOCAL_APPROVAL_ENABLED=false` fails protected actions closed.
`CORE_AGENT_ENVIRONMENT=production` refuses to start with the approve-all stub; inject a real
operator control plane first. Workspaces default to `/tmp/core-agent/runs` and can be moved with
`LOCAL_WORKSPACE_ROOT`.

For a local Docker smoke run, copy `.env.example` to `.env`, set the database, model credentials,
and optional `CORE_AGENT_PROFILE`, then run:

```bash
docker compose up --build
```

Compose waits for PostgreSQL, runs `core-agent-db migrate` as a one-shot job, then starts the agent
with PostgreSQL persistence. It also starts the pinned Arize Phoenix UI at
`http://localhost:6006`, stores Phoenix data in the `phoenix` PostgreSQL schema, and sends Core Agent
and Memory Service traces to its OTLP/HTTP collector. `PHOENIX_PORT` changes the host UI port and
`PHOENIX_DEFAULT_RETENTION_POLICY_DAYS` controls trace retention. The applications use the standard
per-signal `OTEL_EXPORTER_OTLP_*_ENDPOINT` variables, so a production deployment can route traces,
metrics, and logs to separate backends without sending unsupported signals to Phoenix.
The local profile sets `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`, so Phoenix renders
the agent system prompt, available tool schemas, model input/output, tool calls/results, and token
usage as OpenInference AGENT/LLM/TOOL spans. Set it to `false` to verify the production-safe view;
production must opt in only with explicit access, redaction, sampling, and retention policy. Raw
chain-of-thought and credentials are never captured.

Compose deliberately uses the development approve-all operator stub; a production deployment must
replace that control plane and set `CORE_AGENT_ENVIRONMENT=production`.

The specification and acceptance suite are frozen together before implementation changes.
`tests/test_spec_lock.py` also protects every specification file byte-for-byte.

## Memory MCP Service

```bash
MEMORY_ROOT=/data/memory uv run core-agent-memory
```

The service exposes `memory.search`, `read`, `create`, `update`, `split`, `move`, `delete`,
`history`, `index_status`, and `entity_resolve` over MCP Streamable HTTP. Markdown is canonical;
derived retrieval and entity indexes are rebuilt on startup.

## A2A binding

`core_agent.a2a_sdk.build_starlette_app` wraps a runtime handler with the official A2A 1.0
HTTP+JSON routes, Agent Card, streaming, polling, subscription, and task operations. Production
deployments should pass their authenticated call-context builder and durable SDK task store.

Terminal commands, skills, and stdio-MCP processes run through
`core_agent.execution.LocalTerminalBackend`. Each main or child agent gets a separate PTY,
process group, clean environment, and workspace copy inside the single application container.
These sessions are operationally separated but share one OS security boundary. An S3-backed mount
is reserved for durable snapshots, checkpoints, and artifacts rather than active workspaces.
