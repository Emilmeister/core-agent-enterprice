from __future__ import annotations

import asyncio
import uuid

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
    Message as SdkMessage,
    Role as SdkRole,
    Task as SdkTask,
    TaskState as SdkTaskState,
    TaskStatus as SdkTaskStatus,
)
from starlette.applications import Starlette
from a2a.utils.errors import TaskNotCancelableError

from .a2a import (
    Artifact,
    LOCAL_APPROVAL_STATUS_URI,
    Message,
    Part,
    parse_run_request,
)
from .runtime import ApprovalNeeded


LOCAL_APPROVAL_WAIT_TEXT = (
    "Execution is waiting for authorization from the serving agent's local "
    "operator. The caller cannot approve or deny this action. No protected side "
    "effect has been executed. Wait for a task update, poll GetTask, subscribe "
    "to the task, or cancel the task."
)
LOCAL_APPROVAL_GRANTED_TEXT = (
    "The serving agent's local operator authorized the protected action. "
    "Execution is reserved and will start after final policy and digest checks."
)
LOCAL_APPROVAL_LOCKED_TEXT = (
    "The task is locked while awaiting authorization from the serving agent's "
    "local operator. The caller cannot resolve or modify this authorization. "
    "No protected side effect has been executed. The message was not applied; "
    "use GetTask, SubscribeToTask, push "
    "notifications, or CancelTask."
)


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
    def __init__(
        self,
        handler,
        local_approval_reserve_handler,
        local_approval_dispatch_handler,
        cancel_handler,
        is_waiting_local_approval,
        local_approval_extension_uri,
    ):
        self.handler = handler
        self.local_approval_reserve_handler = local_approval_reserve_handler
        self.local_approval_dispatch_handler = local_approval_dispatch_handler
        self.cancel_handler = cancel_handler
        self.is_waiting_local_approval = is_waiting_local_approval
        self.local_approval_extension_uri = local_approval_extension_uri

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

    def _local_approval_message(
        self, context, approval, *, phase="awaiting_local_operator"
    ):
        extension_aware = (
            self.local_approval_extension_uri in context.requested_extensions
        )
        payload = approval.to_public_payload(
            phase=phase,
            status_version=approval.request.version
            + (phase != "awaiting_local_operator"),
        )
        message = SdkMessage(
            message_id=str(uuid.uuid4()),
            task_id=context.task_id,
            context_id=context.context_id,
            role=SdkRole.ROLE_AGENT,
            parts=[
                SdkPart(
                    text=(
                        LOCAL_APPROVAL_WAIT_TEXT
                        if phase == "awaiting_local_operator"
                        else LOCAL_APPROVAL_GRANTED_TEXT
                    ),
                    media_type="text/plain",
                )
            ],
        )
        if extension_aware:
            message.metadata.update({self.local_approval_extension_uri: payload})
            message.extensions.append(self.local_approval_extension_uri)
        return message

    async def execute(self, context, event_queue):
        if context.current_task is not None and self.is_waiting_local_approval(
            context.task_id
        ):
            await TaskUpdater(
                event_queue, context.task_id, context.context_id
            ).update_status(
                SdkTaskState.TASK_STATE_WORKING,
                message=SdkMessage(
                    message_id=str(uuid.uuid4()),
                    task_id=context.task_id,
                    context_id=context.context_id,
                    role=SdkRole.ROLE_AGENT,
                    parts=[
                        SdkPart(
                            text=LOCAL_APPROVAL_LOCKED_TEXT,
                            media_type="text/plain",
                        )
                    ],
                ),
                metadata={
                    "reason": "TASK_LOCKED_AWAITING_LOCAL_OPERATOR",
                    "callerActionRequired": False,
                },
            )
            return
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
            command = parse_run_request(message, context.requested_extensions)
            artifact = await asyncio.to_thread(self.handler, command, context)
            while isinstance(artifact, ApprovalNeeded):
                await updater.update_status(
                    SdkTaskState.TASK_STATE_WORKING,
                    message=self._local_approval_message(context, artifact),
                    metadata={"reason": "awaiting_local_operator"},
                )
                pending = artifact
                reserved = await asyncio.to_thread(
                    self.local_approval_reserve_handler, pending, context
                )
                await updater.update_status(
                    SdkTaskState.TASK_STATE_WORKING,
                    message=self._local_approval_message(
                        context, pending, phase="local_operator_approved"
                    ),
                    metadata={"reason": "local_operator_approved"},
                )
                artifact = await asyncio.to_thread(
                    self.local_approval_dispatch_handler, reserved, context
                )
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
        await asyncio.to_thread(self.cancel_handler, context)
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


class CoreRequestHandler(DefaultRequestHandler):
    def __init__(self, *args, can_cancel, **kwargs):
        super().__init__(*args, **kwargs)
        self.can_cancel = can_cancel

    async def on_cancel_task(self, params, context):
        if not self.can_cancel(params.id):
            raise TaskNotCancelableError(
                message="Task has a committed local-approval execution reservation"
            )
        return await super().on_cancel_task(params, context)


def build_starlette_app(
    *,
    agent_card,
    handler,
    local_approval_reserve_handler,
    local_approval_dispatch_handler,
    cancel_handler,
    is_waiting_local_approval,
    can_cancel,
    base_url,
    context_builder=None,
    task_store=None,
):
    """Build the official A2A 1.0 HTTP+JSON binding around the domain runtime."""
    sdk_card = to_sdk_agent_card(agent_card, base_url=base_url)
    request_handler = CoreRequestHandler(
        CoreAgentExecutor(
            handler,
            local_approval_reserve_handler,
            local_approval_dispatch_handler,
            cancel_handler,
            is_waiting_local_approval,
            agent_card.optional_extensions[0]
            if agent_card.optional_extensions
            else LOCAL_APPROVAL_STATUS_URI,
        ),
        task_store or InMemoryTaskStore(),
        sdk_card,
        can_cancel=can_cancel,
    )
    routes = create_agent_card_routes(sdk_card)
    routes.extend(create_rest_routes(request_handler, context_builder=context_builder))
    return Starlette(routes=routes)
