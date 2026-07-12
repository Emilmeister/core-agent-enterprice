import base64
import asyncio
import hashlib
import hmac
import json
import os
import time
import unittest
import uuid
from unittest.mock import patch

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import GetTaskRequest, Role, SendMessageRequest, TaskState
from a2a.utils.constants import TransportProtocol
from starlette.applications import Starlette

from core_agent.app import create_app
from core_agent.a2a import CORE_EXTENSION_URI
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.operator import (
    OperatorAuthenticator,
    PrivateOperatorControlPlane,
    operator_routes,
)


def _encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def token(secret, *, roles=("agent_operator",), audience="operator-api"):
    header = _encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _encode(
        json.dumps(
            {
                "iss": "operator-issuer",
                "aud": audience,
                "sub": "operator-1",
                "jti": "session-1",
                "roles": list(roles),
                "exp": time.time() + 60,
            }
        ).encode()
    )
    signature = _encode(
        hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    )
    return f"{header}.{payload}.{signature}"


class ResumeHandler:
    def __init__(self):
        self.calls = []

    async def resume_task(self, task_id, context_id, context):
        self.calls.append((task_id, context_id, context.tenant, context.user.user_name))


class OperatorControlPlaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_separate_jwt_authority_reserves_exact_action_and_wakes_task(self):
        secret = "x" * 32
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "call-1",
                            "core.terminal.exec",
                            {"argv": ["python", "-c", "print('protected')"]},
                        ),
                    )
                )
            ]
        )
        model.model = "operator-test"
        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_STATE_BACKEND": "test",
                "CORE_AGENT_TRUST_TERMINAL": "0",
                "CORE_AGENT_APPROVAL_MODE": "on_risk",
                "LOCAL_APPROVAL_DB_PATH": ":memory:",
            },
            clear=True,
        ):
            core_app = create_app(
                model=model, control_plane=PrivateOperatorControlPlane()
            )
        agent = core_app.state.core_agent
        pending = agent.run(
            {"prompt": "protected", "mcp": [], "skills": []},
            task_id="task-1",
            identity="caller-1",
            session_id="context-1",
            tenant_id="tenant-1",
        )
        resume = ResumeHandler()
        auth = OperatorAuthenticator(
            secret, issuer="operator-issuer", audience="operator-api"
        )
        app = Starlette(routes=operator_routes(agent, resume, auth))
        headers = {"Authorization": f"Bearer {token(secret)}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://operator"
        ) as client:
            unauthorized = await client.get(
                "/internal/approvals",
                headers={"Authorization": f"Bearer {token(secret, roles=('caller',))}"},
            )
            self.assertEqual(unauthorized.status_code, 401)
            listed = await client.get("/internal/approvals", headers=headers)
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["items"][0]["id"], pending.request.id)
            approved = await client.post(
                f"/internal/approvals/{pending.request.id}:approve",
                headers={**headers, "If-Match": str(pending.request.version)},
                json={"action_digest": pending.request.action_digest},
            )
        try:
            self.assertEqual(approved.status_code, 202)
            self.assertEqual(
                agent.workflow_store.lookup_task("task-1").state,
                "APPROVED_RESERVED",
            )
            self.assertEqual(
                resume.calls,
                [("task-1", "context-1", "tenant-1", "caller-1")],
            )
        finally:
            core_app.state.close()

    async def test_operator_decision_resumes_detached_a2a_task(self):
        secret = "y" * 32
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "call-a2a",
                            "core.terminal.exec",
                            {"argv": ["python", "-c", "print('approved')"]},
                        ),
                    )
                ),
                ModelResponse(message="completed-after-operator"),
            ]
        )
        model.model = "operator-a2a-test"
        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_STATE_BACKEND": "test",
                "CORE_AGENT_TRUST_TERMINAL": "0",
                "CORE_AGENT_APPROVAL_MODE": "on_risk",
                "LOCAL_APPROVAL_DB_PATH": ":memory:",
            },
            clear=True,
        ):
            app = create_app(
                model=model,
                control_plane=PrivateOperatorControlPlane(),
                base_url="http://agent.test",
            )
        auth = OperatorAuthenticator(
            secret, issuer="operator-issuer", audience="operator-api"
        )
        app.routes[0:0] = operator_routes(
            app.state.core_agent, app.state.a2a_request_handler, auth
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://agent.test",
            headers={"A2A-Extensions": CORE_EXTENSION_URI},
        ) as http:
            client = await ClientFactory(
                ClientConfig(
                    streaming=False,
                    httpx_client=http,
                    supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
                )
            ).create_from_url("http://agent.test")
            request = SendMessageRequest()
            request.configuration.return_immediately = True
            request.message.message_id = str(uuid.uuid4())
            request.message.role = Role.ROLE_USER
            request.message.parts.add().text = "protected a2a"
            request.message.extensions.append(CORE_EXTENSION_URI)
            request.message.metadata.update(
                {CORE_EXTENSION_URI: {"mcp": [], "skills": []}}
            )
            events = [event async for event in client.send_message(request)]
            task_id = events[-1].task.id
            auth_headers = {"Authorization": f"Bearer {token(secret)}"}
            approval = None
            for _ in range(50):
                response = await http.get(
                    "/internal/approvals", headers=auth_headers
                )
                items = response.json().get("items", [])
                if items:
                    approval = items[0]
                    break
                await asyncio.sleep(0.01)
            self.assertIsNotNone(approval)
            accepted = await http.post(
                f"/internal/approvals/{approval['id']}:approve",
                headers={**auth_headers, "If-Match": str(approval["version"])},
                json={"action_digest": approval["action_digest"]},
            )
            self.assertEqual(accepted.status_code, 202)
            task = events[-1].task
            for _ in range(500):
                task = await client.get_task(GetTaskRequest(id=task_id))
                if task.status.state == TaskState.TASK_STATE_COMPLETED:
                    break
                await asyncio.sleep(0.01)
        try:
            self.assertEqual(task.status.state, TaskState.TASK_STATE_COMPLETED)
            self.assertEqual(
                [part.text for artifact in task.artifacts for part in artifact.parts],
                ["completed-after-operator"],
            )
        finally:
            app.state.close()


if __name__ == "__main__":
    unittest.main()
