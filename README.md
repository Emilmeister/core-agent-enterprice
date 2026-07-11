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
`CORE_AGENT_TRUST_TERMINAL=0` to require risk approval instead of automatic execution. Workspaces
default to `/tmp/core-agent/runs` and can be moved with `LOCAL_WORKSPACE_ROOT`.

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
