# Core Agent

Policy-enforced Python agent runtime implemented from the immutable [product specification](spec/README.md).
Its task input contains only `prompt`, `mcp`, and `skills`; platform policy, kernel behavior,
approvals, isolation, durability, context management, tasks, and telemetry remain inside the runtime.

## Development

```bash
uv sync
uv run python -m unittest discover -s tests -v
uvx ruff check core_agent memory_service
uv build --no-sources
```

The specification and acceptance suite were frozen before implementation. `tests/test_spec_lock.py`
also protects every specification file byte-for-byte.

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

Terminal commands and skill or stdio-MCP processes belong behind
`core_agent.execution.RemoteExecutionBackend`; the control plane has no host-execution fallback.
