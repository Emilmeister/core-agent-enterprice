from __future__ import annotations

import asyncio
import uuid

from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_rest_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities as SdkAgentCapabilities,
    AgentCard as SdkAgentCard,
    AgentExtension as SdkAgentExtension,
    AgentInterface as SdkAgentInterface,
    AgentSkill as SdkAgentSkill,
    Part as SdkPart,
    Message as SdkMessage,
    Role as SdkRole,
    Task as SdkTask,
    TaskState as SdkTaskState,
    TaskStatus as SdkTaskStatus,
)
from starlette.applications import Starlette

from .a2a import (
    APPROVAL_REQUEST_URI,
    APPROVAL_RESPONSE_URI,
    Artifact,
    Message,
    Part,
    parse_approval_decision,
    parse_run_request,
)
from .runtime import ApprovalNeeded


def to_sdk_agent_card(card, *, base_url):
    capabilities = SdkAgentCapabilities(
        streaming=bool(card.capabilities.get("streaming")),
        push_notifications=bool(card.capabilities.get("pushNotifications")),
        extensions=[
            *(SdkAgentExtension(uri=uri, required=True) for uri in card.extensions),
            *(
                SdkAgentExtension(uri=uri, required=False)
                for uri in card.optional_extensions
            ),
        ],
    )
    return SdkAgentCard(
        name=card.name,
        description="Policy-enforced core agent runtime",
        version="1.0.0",
        supported_interfaces=[
            SdkAgentInterface(
                url=base_url,
                protocol_binding=binding,
                protocol_version=version,
            )
            for version in card.protocol_versions
            for binding in card.bindings
        ],
        capabilities=capabilities,
        default_input_modes=card.input_modes,
        default_output_modes=card.output_modes,
        skills=[
            SdkAgentSkill(
                id=name, name=name, description=f"Enabled agent capability: {name}"
            )
            for name in card.skills
        ],
    )


class CoreAgentExecutor(AgentExecutor):
    def __init__(self, handler):
        self.handler = handler

    @staticmethod
    def _message(context):
        message = context.message
        metadata = json_format.MessageToDict(message.metadata) if message else {}
        parts = []
        for part in message.parts if message else ():
            if part.text:
                parts.append(Part.text(part.text))
            elif part.HasField("data"):
                parts.append(Part("data", json_format.MessageToDict(part.data)))
        return Message(
            role="user",
            parts=tuple(parts),
            extensions=tuple(message.extensions if message else ()),
            metadata=metadata,
            context_id=context.context_id,
        )

    @staticmethod
    def _approval_message(context, approval):
        payload = approval.to_payload()
        data = json_format.ParseDict(payload, Value())
        return SdkMessage(
            message_id=str(uuid.uuid4()),
            task_id=context.task_id,
            context_id=context.context_id,
            role=SdkRole.ROLE_AGENT,
            parts=[SdkPart(data=data, media_type="application/json")],
            metadata={APPROVAL_REQUEST_URI: payload},
            extensions=[APPROVAL_REQUEST_URI],
        )

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
        try:
            message = self._message(context)
            command = (
                parse_approval_decision(message, context.requested_extensions)
                if APPROVAL_RESPONSE_URI in message.extensions
                else parse_run_request(message, context.requested_extensions)
            )
            artifact = await asyncio.to_thread(self.handler, command, context)
            if isinstance(artifact, ApprovalNeeded):
                if APPROVAL_REQUEST_URI not in context.requested_extensions:
                    raise ValueError(
                        "client did not negotiate approval request extension"
                    )
                await updater.update_status(
                    SdkTaskState.TASK_STATE_INPUT_REQUIRED,
                    message=self._approval_message(context, artifact),
                    metadata={"reason": "approval_required"},
                )
                return
            if not isinstance(artifact, Artifact):
                artifact = Artifact.text(str(artifact))
            await updater.add_artifact(
                [
                    SdkPart(text=str(part.data), media_type=artifact.media_type)
                    for part in artifact.parts
                ],
                artifact_id=artifact.id,
                metadata={
                    "digest": artifact.digest,
                    "size": artifact.size,
                    "provenance": artifact.provenance,
                },
                last_chunk=True,
            )
            await updater.complete()
        except Exception:
            await updater.failed()
            raise

    async def cancel(self, context, event_queue):
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def build_starlette_app(
    *, agent_card, handler, base_url, context_builder=None, task_store=None
):
    """Build the official A2A 1.0 HTTP+JSON binding around the domain runtime."""
    sdk_card = to_sdk_agent_card(agent_card, base_url=base_url)
    request_handler = DefaultRequestHandler(
        CoreAgentExecutor(handler),
        task_store or InMemoryTaskStore(),
        sdk_card,
    )
    routes = create_agent_card_routes(sdk_card)
    routes.extend(create_rest_routes(request_handler, context_builder=context_builder))
    return Starlette(routes=routes)
