from __future__ import annotations

import asyncio

from google.protobuf import json_format

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
    Task as SdkTask,
    TaskState as SdkTaskState,
    TaskStatus as SdkTaskStatus,
)
from starlette.applications import Starlette

from .a2a import Artifact, Message, Part, parse_run_request


def to_sdk_agent_card(card, *, base_url):
    capabilities = SdkAgentCapabilities(
        streaming=bool(card.capabilities.get("streaming")),
        push_notifications=bool(card.capabilities.get("pushNotifications")),
        extensions=[
            SdkAgentExtension(uri=uri, required=True) for uri in card.extensions
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
        parts = tuple(
            Part.text(part.text)
            for part in (message.parts if message else ())
            if part.text
        )
        return Message(
            role="user",
            parts=parts,
            extensions=tuple(message.extensions if message else ()),
            metadata=metadata,
            context_id=context.context_id,
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
            request = parse_run_request(
                self._message(context), context.requested_extensions
            )
            artifact = await asyncio.to_thread(self.handler, request, context)
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
