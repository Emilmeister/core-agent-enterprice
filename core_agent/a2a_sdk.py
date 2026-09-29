from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.request_handlers.request_handler import (
    validate,
    validate_request_params,
)
from a2a.server.routes import (
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, DEFAULT_RPC_URL
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities as SdkAgentCapabilities,
    AgentCard as SdkAgentCard,
    AgentExtension as SdkAgentExtension,
    AgentInterface as SdkAgentInterface,
    AgentSkill as SdkAgentSkill,
    Part as SdkPart,
    Role as SdkRole,
    Task as SdkTask,
    SubscribeToTaskRequest,
    TaskState as SdkTaskState,
    TaskStatus as SdkTaskStatus,
    TaskStatusUpdateEvent as SdkTaskStatusUpdateEvent,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, request_response
from a2a.utils.errors import (
    InvalidParamsError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from a2a.utils.task import apply_history_length, validate_history_length

from .a2a import (
    Artifact,
    Message,
    Part,
    parse_run_request,
)
from .auth import OWNER_SCOPE, ScopeUser, is_company_owner
from .streaming import (
    ADK_THOUGHT_KEY,
    ADK_TYPE_KEY,
    DEFAULT_BUFFER_SIZE,
    FUNCTION_CALL_TYPE,
    FUNCTION_RESPONSE_TYPE,
    PARTIAL_KEY,
    NullStreamPublisher,
    StreamBuffer,
)


A2A_TERMINAL_STATES = {
    SdkTaskState.TASK_STATE_COMPLETED,
    SdkTaskState.TASK_STATE_FAILED,
    SdkTaskState.TASK_STATE_CANCELED,
    SdkTaskState.TASK_STATE_REJECTED,
}



def resolve_owner_scope(context):
    name = context.user.user_name
    return name if context.user.is_authenticated and name else "anonymous"


class ScopedMemoryTaskStore(InMemoryTaskStore):
    """Development adapter: owner access with the same company boundary as SQL."""

    def __init__(self):
        super().__init__(owner_resolver=lambda context: json.dumps([
            context.tenant, resolve_owner_scope(context),
        ]))
        self._owners = {}
        self._access_lock = asyncio.Lock()

    @staticmethod
    def _as_owner(context, owner):
        return context.model_copy(update={"user": ScopeUser(owner)})

    async def save(self, task, context):
        async with self._access_lock:
            key = (context.tenant, task.id)
            caller = resolve_owner_scope(context)
            owner = self._owners.get(key, caller)
            if owner != caller and not is_company_owner(context):
                raise InvalidParamsError("Task not found")
            self._owners[key] = owner
            await super().save(task, self._as_owner(context, owner))
            if owner != OWNER_SCOPE:
                await super().save(task, self._as_owner(context, OWNER_SCOPE))

    async def get(self, task_id, context):
        async with self._access_lock:
            owner = self._owners.get((context.tenant, task_id))
            if owner is None:
                return None
            if is_company_owner(context):
                # Follow-up/cancel keep the task's original execution identity.
                context.user = ScopeUser(owner)
            elif owner != resolve_owner_scope(context):
                return None
            return await super().get(task_id, context)

    async def list(self, params, context):
        async with self._access_lock:
            if is_company_owner(context):
                context = self._as_owner(context, OWNER_SCOPE)
            return await super().list(params, context)

    async def delete(self, task_id, context):
        async with self._access_lock:
            key = (context.tenant, task_id)
            owner = self._owners.get(key)
            if owner is None or (owner != resolve_owner_scope(context) and not is_company_owner(context)):
                return
            await super().delete(task_id, self._as_owner(context, owner))
            if owner != OWNER_SCOPE:
                await super().delete(task_id, self._as_owner(context, OWNER_SCOPE))
            del self._owners[key]


def to_sdk_agent_card(card, *, base_url):
    capabilities = SdkAgentCapabilities(
        streaming=bool(card.capabilities.get("streaming")),
        push_notifications=bool(card.capabilities.get("pushNotifications")),
        extensions=[
            *(SdkAgentExtension(uri=uri, required=True) for uri in card.extensions),
            *(
                SdkAgentExtension(
                    uri=uri,
                    required=False,
                    description=(
                        "Reports private local-operator authorization wait; the "
                        "A2A caller cannot resolve it."
                    ),
                    params={
                        "authorizationOwner": "serving_agent_local_operator",
                        "callerCanResolve": False,
                        "publicTaskState": "TASK_STATE_WORKING",
                        "decisionTransport": "private_out_of_band",
                    },
                )
                for uri in card.optional_extensions
            ),
        ],
    )
    return SdkAgentCard(
        name=card.name,
        description=card.description,
        version=card.version,
        supported_interfaces=[
            SdkAgentInterface(
                url=base_url,
                protocol_binding=binding,
                protocol_version=version,
            )
            for binding, version in card.interfaces
        ],
        capabilities=capabilities,
        default_input_modes=card.input_modes,
        default_output_modes=card.output_modes,
        skills=[
            SdkAgentSkill(
                id=name,
                name=name,
                description=f"Enabled agent capability: {name}",
                # REQUIRED in A2A 1.0; protobuf JSON drops an empty list entirely.
                tags=[name.removeprefix("core_").split("_")[0]],
            )
            for name in card.skills
        ],
    )


LEGACY_AGENT_CARD_PATH = "/.well-known/agent.json"


def public_base_url(request):
    """Derive the reachable base URL of this agent from one card request.

    A card advertising http://localhost:PORT is discoverable but uncallable, and
    the deployment cannot know its public address by itself. Returns None when the
    headers carry nothing usable, so the caller keeps the configured value.
    """

    def first(name):
        return (request.headers.get(name) or "").split(",")[0].strip()

    # Each header stands on its own. A TLS-terminating proxy that already passes the
    # public name in `Host` sends no `X-Forwarded-Host`, and tying the scheme to that
    # header advertises http:// for an https-only deployment.
    # Each header stands on its own. A TLS-terminating proxy that already passes the
    # public name in `Host` sends no `X-Forwarded-Host`, and tying the scheme to that
    # header advertises http:// for an https-only deployment.
    scheme = first("x-forwarded-proto") or request.url.scheme
    host = first("x-forwarded-host") or first("host")
    if not host or scheme not in ("http", "https"):
        return None
    # "@" would turn the advertised URL into one carrying userinfo; whitespace and
    # control bytes would make it an invalid URL or split the header.
    if "@" in host or any(char.isspace() or ord(char) < 0x20 for char in host):
        return None
    return f"{scheme}://{host}"


JSONRPC_ENVELOPE_FIELDS = ("jsonrpc", "method", "params", "id")
_reported_extra_fields = set()


def _strip_envelope(payload):
    """Drop unknown top-level members so a hedging client is not rejected outright.

    The dropped value is never interpreted: a top-level contextId must not become
    a second, undocumented way to set the session.
    """
    if isinstance(payload, list):
        return [_strip_envelope(item) for item in payload]
    if not isinstance(payload, dict):
        return payload
    extra = frozenset(payload) - frozenset(JSONRPC_ENVELOPE_FIELDS)
    if not extra:
        return payload
    if extra not in _reported_extra_fields and len(_reported_extra_fields) < 32:
        _reported_extra_fields.add(extra)
        logging.getLogger("core_agent.runtime").warning(
            "ignoring unknown JSON-RPC fields %s; they are not interpreted",
            ",".join(sorted(extra)),
        )
    return {name: payload[name] for name in payload if name not in extra}


def _tolerant_envelope(endpoint):
    async def wrapper(request):
        body = await request.body()
        try:
            payload = json.loads(body)
        except ValueError:
            return await endpoint(request)
        cleaned = _strip_envelope(payload)
        if cleaned == payload:
            return await endpoint(Request(request.scope, _replay(body)))
        return await endpoint(
            Request(request.scope, _replay(json.dumps(cleaned).encode()))
        )

    return wrapper


def _replay(body):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _agent_card_routes(sdk_card, *, derive_base_url):
    """Serve the card on the canonical and the historical path.

    Registries written before the card was renamed still probe the historical
    path; a 404 there makes the agent undiscoverable rather than degraded.
    """

    async def endpoint(request):
        card = sdk_card
        base_url = public_base_url(request) if derive_base_url else None
        prefix = request.scope.get("root_path", "")
        authenticated = "principal" in request.scope
        if base_url or prefix or authenticated:
            card = SdkAgentCard()
            card.CopyFrom(sdk_card)
            for interface in card.supported_interfaces:
                interface.url = (base_url or interface.url).rstrip("/") + prefix
            if authenticated:
                scheme = card.security_schemes["keycloak"]
                scheme.http_auth_security_scheme.scheme = "bearer"
                card.security_requirements.add().schemes["keycloak"].SetInParent()
        return JSONResponse(json_format.MessageToDict(card))

    return [
        Route(path, endpoint, methods=["GET"])
        for path in (AGENT_CARD_WELL_KNOWN_PATH, LEGACY_AGENT_CARD_PATH)
    ]


STREAM_FAILURE_SEPARATOR = "\n\n---\n"


def _chunk(text, size):
    """Split a result into MAX_CHUNK_SIZE artifact chunks; 0 means one chunk."""
    if size <= 0 or len(text) <= size:
        return [text]
    return [text[start : start + size] for start in range(0, len(text), size)]


def _struct_value(value):
    """Wrap an arbitrary JSON-compatible value as a protobuf Value."""
    wrapper = Value()
    json_format.ParseDict(value, wrapper)
    return wrapper


class TaskStreamPublisher:
    """Publish runtime progress from the worker thread onto the A2A event queue.

    The agent loop is synchronous and runs under ``asyncio.to_thread``; every
    emission is therefore marshalled back onto the serving event loop and awaited
    so frames keep their order and the producer inherits the queue's back-pressure.
    """

    enabled = True

    def __init__(self, updater, loop, *, buffer_size=DEFAULT_BUFFER_SIZE, timeout=30):
        self.updater = updater
        self.loop = loop
        self.buffer = StreamBuffer(buffer_size)
        self.timeout = timeout
        self.closed = False

    @property
    def streamed_text(self):
        return self.buffer.response

    def _submit(self, coroutine):
        if self.closed:
            coroutine.close()
            return
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            future.result(self.timeout)
        except RuntimeError:
            # The task already reached a terminal state; stop relaying quietly.
            self.closed = True

    def _publish(self, parts, *, partial=False):
        metadata = {PARTIAL_KEY: True} if partial else None
        message = self.updater.new_agent_message(parts, metadata=metadata)
        self._submit(
            self.updater.update_status(SdkTaskState.TASK_STATE_WORKING, message=message)
        )

    @staticmethod
    def _text_parts(response, reasoning):
        parts = []
        if reasoning:
            part = SdkPart(text=reasoning, media_type="text/plain")
            part.metadata.update({ADK_THOUGHT_KEY: True})
            parts.append(part)
        if response:
            parts.append(SdkPart(text=response, media_type="text/plain"))
        return parts

    def text(self, response, reasoning):
        snapshot = self.buffer.update(response, reasoning)
        if snapshot is None:
            return
        parts = self._text_parts(*snapshot)
        if parts:
            self._publish(parts, partial=True)

    def flush(self):
        snapshot = self.buffer.flush()
        if snapshot is None:
            return
        parts = self._text_parts(*snapshot)
        if parts:
            self._publish(parts, partial=True)

    def _data_part(self, payload, adk_type):
        part = SdkPart(data=_struct_value(payload), media_type="application/json")
        part.metadata.update({ADK_TYPE_KEY: adk_type})
        return part

    def tool_call(self, call_id, name, arguments):
        self.flush()
        self.buffer.reset()
        self._publish(
            [
                self._data_part(
                    {"id": call_id, "name": name, "args": arguments},
                    FUNCTION_CALL_TYPE,
                )
            ]
        )

    def tool_result(self, call_id, name, response):
        self._publish(
            [
                self._data_part(
                    {"id": call_id, "name": name, "response": response},
                    FUNCTION_RESPONSE_TYPE,
                )
            ]
        )

    def relay(self, parts):
        """Forward already-shaped downstream parts into this task's stream."""
        published = []
        for part in parts:
            if isinstance(part.get("text"), str) and part["text"]:
                item = SdkPart(text=part["text"], media_type="text/plain")
            elif part.get("data") is not None:
                item = SdkPart(
                    data=_struct_value(part["data"]), media_type="application/json"
                )
            else:
                continue
            if isinstance(part.get("metadata"), dict):
                item.metadata.update(part["metadata"])
            published.append(item)
        if published:
            self._publish(published, partial=True)


class CoreAgentExecutor(AgentExecutor):
    def __init__(
        self,
        handler,
        cancel_handler,
        resume_handler,
        cancel_signal=None,
        stream_buffer_size=DEFAULT_BUFFER_SIZE,
        streaming_enabled=True,
        max_chunk_size=0,
    ):
        self.handler = handler
        self.cancel_handler = cancel_handler
        self.cancel_signal = cancel_signal
        self.resume_handler = resume_handler
        self.stream_buffer_size = stream_buffer_size
        self.streaming_enabled = streaming_enabled
        self.max_chunk_size = max_chunk_size
        self._cancel_publications = {}
        self._active_executions = set()

    @staticmethod
    def _from_sdk_message(message, *, context_id=None):
        metadata = json_format.MessageToDict(message.metadata) if message else {}
        parts = []
        for part in message.parts if message else ():
            if part.text:
                parts.append(Part.text(part.text))
            elif part.HasField("data"):
                parts.append(Part("data", json_format.MessageToDict(part.data)))
            elif part.HasField("raw"):
                parts.append(
                    Part.file(
                        part.raw, filename=part.filename, media_type=part.media_type
                    )
                )
            elif part.HasField("url"):
                # Kept as an unsupported kind on purpose: fetching a caller-chosen
                # URL would be SSRF with the agent's network reach.
                parts.append(Part("url", part.url))
        return Message(
            role="user",
            parts=tuple(parts),
            extensions=tuple(message.extensions if message else ()),
            metadata=metadata,
            context_id=(
                message.context_id if message and message.context_id else context_id
            ),
            message_id=message.message_id if message else None,
            task_id=message.task_id if message and message.task_id else None,
        )

    @classmethod
    def _message(cls, context):
        return cls._from_sdk_message(context.message, context_id=context.context_id)

    async def execute(self, context, event_queue):
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if context.current_task is None:
            await event_queue.enqueue_event(
                SdkTask(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=SdkTaskStatus(state=SdkTaskState.TASK_STATE_SUBMITTED),
                    history=[context.message] if context.message else [],
                )
            )
        await updater.start_work()
        publisher = (
            TaskStreamPublisher(
                updater,
                asyncio.get_running_loop(),
                buffer_size=self.stream_buffer_size,
            )
            if self.streaming_enabled
            else NullStreamPublisher()
        )
        self._active_executions.add(context.task_id)
        try:
            if context.message is None and context.current_task is not None:
                worker = asyncio.create_task(
                    asyncio.to_thread(self.resume_handler, context, publisher)
                )
            else:
                message = self._message(context)
                command = parse_run_request(message)
                worker = asyncio.create_task(
                    asyncio.to_thread(self.handler, command, context, publisher)
                )
            try:
                artifact = await asyncio.shield(worker)
            except asyncio.CancelledError:
                publication = self._cancel_publications.get(context.task_id)
                if publication is None:
                    raise
                try:
                    artifact = await worker
                finally:
                    await publication.wait()
            publisher.closed = True
            artifact_publication = asyncio.create_task(
                self._publish_artifact(updater, artifact)
            )
            try:
                await asyncio.shield(artifact_publication)
            except asyncio.CancelledError:
                publication = self._cancel_publications.get(context.task_id)
                if publication is None:
                    artifact_publication.cancel()
                    await asyncio.gather(artifact_publication, return_exceptions=True)
                    raise
                try:
                    await artifact_publication
                finally:
                    await publication.wait()
        except Exception as error:
            publisher.closed = True
            code = getattr(error, "code", None)
            if code == "LEASE_LOST" or (
                code == "WORKER_STOPPED"
                and getattr(error, "data", {}).get("workflow_admitted")
            ):
                return
            if code == "TASK_CANCELLED":
                try:
                    await updater.cancel()
                except RuntimeError:
                    pass
                return
            await self._publish_failure(updater, publisher, error)
            raise
        finally:
            self._active_executions.discard(context.task_id)
            self._cancel_publications.pop(context.task_id, None)

    async def _publish_artifact(self, updater, artifact):
        if not isinstance(artifact, Artifact):
            artifact = Artifact.text(str(artifact))
        final_text = "\n".join(str(part.data) for part in artifact.parts)
        chunks = _chunk(final_text, self.max_chunk_size)
        for index, chunk in enumerate(chunks):
            await updater.add_artifact(
                [SdkPart(text=chunk, media_type=artifact.media_type)],
                artifact_id=artifact.id,
                metadata={
                    "digest": artifact.digest,
                    "size": artifact.size,
                    "provenance": artifact.provenance,
                },
                append=index > 0 or None,
                last_chunk=index == len(chunks) - 1,
            )
        await updater.complete(
            message=updater.new_agent_message(
                [SdkPart(text=final_text, media_type=artifact.media_type)]
            )
            if final_text
            else None
        )

    @staticmethod
    async def _publish_failure(updater, publisher, error):
        """Keep already-streamed text and append a safe reason, like the ADK stream."""
        reason = getattr(error, "code", None) or type(error).__name__
        streamed = getattr(publisher, "streamed_text", "")
        text = f"{streamed}{STREAM_FAILURE_SEPARATOR}{reason}" if streamed else reason
        try:
            await updater.failed(
                message=updater.new_agent_message(
                    [SdkPart(text=text, media_type="text/plain")]
                )
            )
        except RuntimeError:
            # A terminal state was already published; nothing further may be sent.
            return

    async def cancel(self, context, event_queue):
        publication = None
        if context.task_id in self._active_executions:
            publication = self._cancel_publications.setdefault(
                context.task_id, asyncio.Event()
            )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        try:
            if self.cancel_signal is not None:
                self.cancel_signal(context)
            try:
                await asyncio.to_thread(self.cancel_handler, context)
            except Exception as error:
                if getattr(error, "code", None) != "TASK_NOT_CANCELABLE":
                    raise
                if publication is None:
                    await self._restore_terminal(context, updater)
                return
            await updater.cancel()
        finally:
            if publication is not None:
                publication.set()

    async def _restore_terminal(self, context, updater):
        publisher = NullStreamPublisher()
        try:
            artifact = await asyncio.to_thread(self.resume_handler, context, publisher)
        except Exception as error:
            code = getattr(error, "code", None)
            if code == "TASK_CANCELLED":
                try:
                    await updater.cancel()
                except RuntimeError:
                    pass
            elif code not in {"LEASE_LOST", "WORKER_STOPPED"}:
                await self._publish_failure(updater, publisher, error)
            return
        await self._publish_artifact(updater, artifact)


class TransientStatusTaskStore:
    """Keep transient streamed snapshots out of the durable Task history.

    The SDK task manager appends the previous status message to `Task.history` on
    every status update, so persisting each buffered snapshot would rewrite a
    quadratically growing Task. Only the partial text frames are dropped; tool
    calls, tool results and terminal messages stay in the durable history.
    """

    def __init__(self, inner):
        self.inner = inner

    async def save(self, task, context=None):
        kept = [
            message
            for message in task.history
            if message.role != SdkRole.ROLE_AGENT
            or not json_format.MessageToDict(message.metadata).get(PARTIAL_KEY)
        ]
        if len(kept) != len(task.history):
            del task.history[:]
            task.history.extend(kept)
        return await self.inner.save(task, context)

    async def get(self, task_id, context=None):
        return await self.inner.get(task_id, context)

    async def list(self, params, context=None):
        return await self.inner.list(params, context)

    async def delete(self, task_id, context=None):
        return await self.inner.delete(task_id, context)


class CoreRequestHandler(DefaultRequestHandler):
    def __init__(self, *args, followup_handler, **kwargs):
        super().__init__(*args, **kwargs)
        self.followup_handler = followup_handler

    async def _accept_followup(self, params, context):
        validate_history_length(params.configuration)
        task_id = params.message.task_id
        task = await self.task_store.get(task_id, context)
        if task is None:
            raise TaskNotFoundError(message=f"Task {task_id} not found")
        if task.status.state in A2A_TERMINAL_STATES:
            raise UnsupportedOperationError(
                message=f"Task {task_id} is already terminal"
            )
        if params.message.role != SdkRole.ROLE_USER:
            raise InvalidParamsError(message="Follow-up role must be ROLE_USER")
        if params.message.context_id and params.message.context_id != task.context_id:
            raise InvalidParamsError(message="context_id does not match task")
        message = CoreAgentExecutor._from_sdk_message(
            params.message, context_id=task.context_id
        )
        try:
            await asyncio.to_thread(self.followup_handler, message, task, context)
        except Exception as error:
            code = getattr(error, "code", None)
            if code == "TASK_NOT_FOUND":
                raise TaskNotFoundError(message=f"Task {task_id} not found") from None
            if code in {"TASK_TERMINAL", "INVALID_TASK_STATE"}:
                raise UnsupportedOperationError(
                    message=f"Task {task_id} is already terminal"
                ) from None
            if code == "INVALID_REQUEST":
                raise InvalidParamsError(message=str(error)) from None
            raise
        return apply_history_length(task, params.configuration)

    @validate_request_params
    async def on_message_send(self, params, context):
        if params.message.task_id:
            return await self._accept_followup(params, context)
        return await super().on_message_send(params, context)

    @validate_request_params
    @validate(
        lambda self: self._agent_card.capabilities.streaming,
        "Streaming is not supported by the agent",
    )
    async def on_message_send_stream(self, params, context):
        if params.message.task_id:
            yield await self._accept_followup(params, context)
            return
        async for event in super().on_message_send_stream(params, context):
            yield event

    async def on_cancel_task(self, params, context):
        # The SDK registry can return an active cached task without consulting
        # its scoped store. Authorize before any cancellation or cached response.
        task = await self.task_store.get(params.id, context)
        if task is None:
            raise TaskNotFoundError(message="Task not found")
        return await super().on_cancel_task(params, context)

    @validate_request_params
    @validate(
        lambda self: self._agent_card.capabilities.streaming,
        "Streaming is not supported by the agent",
    )
    async def on_subscribe_to_task(self, params: SubscribeToTaskRequest, context):
        task = await self.task_store.get(params.id, context)
        if task is None:
            raise TaskNotFoundError(message=f"Task {params.id} not found")
        existing = await self._active_task_registry.get(params.id)
        live_stream = None
        next_live = None
        if existing is not None and task.status.state not in A2A_TERMINAL_STATES:
            live_stream = existing.subscribe(include_initial_task=False)
            next_live = asyncio.create_task(anext(live_stream))
            await asyncio.sleep(0)
        previous = task.SerializeToString(deterministic=True)
        try:
            yield task
            if task.status.state in A2A_TERMINAL_STATES:
                return
            while True:
                if next_live is None:
                    await asyncio.sleep(0.25)
                else:
                    done, _pending = await asyncio.wait((next_live,), timeout=0.25)
                    if done:
                        try:
                            event = next_live.result()
                        except (StopAsyncIteration, InvalidParamsError):
                            next_live = None
                        else:
                            next_live = None
                            terminal = (
                                isinstance(event, SdkTask)
                                and event.status.state in A2A_TERMINAL_STATES
                            ) or (
                                isinstance(event, SdkTaskStatusUpdateEvent)
                                and event.status.state in A2A_TERMINAL_STATES
                            )
                            if not terminal:
                                next_live = asyncio.create_task(anext(live_stream))
                            yield event
                            if terminal:
                                return
                task = await self.task_store.get(params.id, context)
                if task is None:
                    raise TaskNotFoundError(message=f"Task {params.id} not found")
                current = task.SerializeToString(deterministic=True)
                if current == previous:
                    continue
                previous = current
                if next_live is None or task.status.state in A2A_TERMINAL_STATES:
                    yield task
                    if task.status.state in A2A_TERMINAL_STATES:
                        return
        finally:
            if next_live is not None:
                next_live.cancel()
                await asyncio.gather(next_live, return_exceptions=True)
            if live_stream is not None:
                await live_stream.aclose()


def build_starlette_app(
    *,
    agent_card,
    handler,
    cancel_handler,
    cancel_signal=None,
    base_url,
    derive_base_url=False,
    resume_handler,
    followup_handler,
    context_builder=None,
    task_store=None,
    push_config_store=None,
    push_sender=None,
    shutdown_handler=None,
    stream_buffer_size=DEFAULT_BUFFER_SIZE,
    streaming_enabled=True,
    max_chunk_size=0,
):
    """Build the official A2A 1.0 JSON-RPC and HTTP+JSON bindings around the runtime."""
    sdk_card = to_sdk_agent_card(agent_card, base_url=base_url)
    request_handler = CoreRequestHandler(
        CoreAgentExecutor(
            handler,
            cancel_handler,
            resume_handler,
            cancel_signal=cancel_signal,
            stream_buffer_size=stream_buffer_size,
            streaming_enabled=streaming_enabled,
            max_chunk_size=max_chunk_size,
        ),
        TransientStatusTaskStore(
            task_store or ScopedMemoryTaskStore()
        ),
        sdk_card,
        push_config_store=push_config_store,
        push_sender=push_sender,
        followup_handler=followup_handler,
    )
    routes = _agent_card_routes(sdk_card, derive_base_url=derive_base_url)
    for route in create_jsonrpc_routes(
        request_handler, DEFAULT_RPC_URL, context_builder=context_builder
    ):
        # A client that also repeats a field outside `params` must not be rejected.
        route.endpoint = _tolerant_envelope(route.endpoint)
        route.app = request_response(route.endpoint)
        routes.append(route)
    routes.extend(create_rest_routes(request_handler, context_builder=context_builder))
    lifespan = None
    if push_sender or shutdown_handler:

        @asynccontextmanager
        async def lifespan(_app):
            stop = asyncio.Event()
            worker = (
                asyncio.create_task(
                    push_sender.run(stop), name="push-notification-dispatcher"
                )
                if push_sender
                else None
            )
            try:
                yield
            finally:
                if push_sender:
                    stop.set()
                    await worker
                    await push_sender.close()
                if shutdown_handler:
                    await asyncio.to_thread(shutdown_handler)

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.a2a_request_handler = request_handler
    return app
