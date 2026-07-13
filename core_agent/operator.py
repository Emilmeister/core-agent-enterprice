from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from starlette.responses import JSONResponse
from starlette.routing import Route

from .errors import CoreError


def _decode(value):
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception:
        raise CoreError("OPERATOR_UNAUTHORIZED") from None


@dataclass(frozen=True)
class OperatorIdentity:
    principal_id: str
    session_id: str


@dataclass(frozen=True)
class AuthenticatedControlPlane:
    operator_principal_id: str
    operator_session_id: str
    automatic: bool = False


@dataclass(frozen=True)
class PrivateOperatorControlPlane:
    automatic: bool = False
    trusted_operator_control_plane: bool = True


class OperatorAuthenticator:
    """Small HS256 verifier for the private, separately-audienced operator API."""

    def __init__(self, secret, *, issuer, audience, role="agent_operator", clock=time.time):
        if not isinstance(secret, str) or len(secret.encode()) < 32:
            raise CoreError("CONFIG_INVALID", "operator JWT secret must be at least 32 bytes")
        if not issuer or not audience or not role:
            raise CoreError("CONFIG_INVALID", "operator JWT issuer/audience/role are required")
        self.secret = secret.encode()
        self.issuer = issuer
        self.audience = audience
        self.role = role
        self.clock = clock

    def authenticate(self, request):
        authorization = request.headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token:
            raise CoreError("OPERATOR_UNAUTHORIZED")
        parts = token.split(".")
        if len(parts) != 3:
            raise CoreError("OPERATOR_UNAUTHORIZED")
        try:
            header = json.loads(_decode(parts[0]))
            claims = json.loads(_decode(parts[1]))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise CoreError("OPERATOR_UNAUTHORIZED") from None
        if header.get("alg") != "HS256":
            raise CoreError("OPERATOR_UNAUTHORIZED")
        expected = hmac.new(
            self.secret, f"{parts[0]}.{parts[1]}".encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _decode(parts[2])):
            raise CoreError("OPERATOR_UNAUTHORIZED")
        now = self.clock()
        audience = claims.get("aud", [])
        audience = [audience] if isinstance(audience, str) else audience
        roles = claims.get("roles", [])
        roles = [roles] if isinstance(roles, str) else roles
        if (
            claims.get("iss") != self.issuer
            or self.audience not in audience
            or self.role not in roles
            or not isinstance(claims.get("exp"), (int, float))
            or claims["exp"] <= now
            or claims.get("nbf", 0) > now
            or not claims.get("sub")
            or not claims.get("jti")
        ):
            raise CoreError("OPERATOR_UNAUTHORIZED")
        return OperatorIdentity(str(claims["sub"]), str(claims["jti"]))


class _ResumeUser(User):
    def __init__(self, name):
        self.name = name

    @property
    def is_authenticated(self):
        return True

    @property
    def user_name(self):
        return self.name


def _private_payload(approval):
    return {
        "id": approval.id,
        "task_id": approval.task_id,
        "context_id": approval.proposal.context_id,
        "tenant_id": approval.proposal.tenant_id,
        "state": approval.state,
        "version": approval.version,
        "action_digest": approval.action_digest,
        "expires_at": approval.expires_at,
        "tool": approval.tool_name,
        "target": approval.proposal.target,
        "arguments": approval.arguments,
        "risk": approval.proposal.risk_level,
        "side_effect_class": approval.proposal.side_effect_class,
    }


def operator_routes(agent, request_handler, authenticator):
    async def guarded(request, operation):
        try:
            identity = authenticator.authenticate(request)
            return await operation(request, identity)
        except CoreError as error:
            status = 401 if error.code == "OPERATOR_UNAUTHORIZED" else 409
            return JSONResponse({"code": error.code}, status_code=status)

    async def list_approvals(_request, _identity):
        return JSONResponse(
            {"items": [_private_payload(item) for item in agent.tool_runtime.approvals.list_pending()]}
        )

    async def get_approval(request, _identity):
        return JSONResponse(
            _private_payload(
                agent.tool_runtime.approvals.get(request.path_params["approval_id"])
            )
        )

    async def decision(request, identity, *, approve):
        approval = agent.tool_runtime.approvals.get(
            request.path_params["approval_id"]
        )
        try:
            version = int(request.headers.get("if-match", ""))
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise CoreError("INVALID_REQUEST") from None
        if (
            not isinstance(body, dict)
            or set(body) != {"action_digest"}
            or body["action_digest"] != approval.action_digest
            or version != approval.version
        ):
            raise CoreError("APPROVAL_ARGUMENTS_CHANGED")
        control_plane = AuthenticatedControlPlane(
            identity.principal_id, identity.session_id
        )
        if approve:
            agent.reserve_local_approval(approval.task_id, approval.id, control_plane)
        else:
            agent.deny_local_approval(
                approval.task_id,
                approval.id,
                operator_principal_id=identity.principal_id,
                operator_session_id=identity.session_id,
                continue_run=False,
            )
        workflow = agent.workflow_store.lookup_task(approval.task_id)
        await request_handler.resume_task(
            workflow.task_id,
            workflow.context_id,
            ServerCallContext(
                user=_ResumeUser(workflow.owner_id), tenant=workflow.tenant_id
            ),
        )
        return JSONResponse(
            {"state": "APPROVED_RESERVED" if approve else "REPLANNING"},
            status_code=202,
        )

    async def list_endpoint(request):
        return await guarded(request, list_approvals)

    async def get_endpoint(request):
        return await guarded(request, get_approval)

    async def approve_endpoint(request):
        return await guarded(
            request, lambda req, identity: decision(req, identity, approve=True)
        )

    async def deny_endpoint(request):
        return await guarded(
            request, lambda req, identity: decision(req, identity, approve=False)
        )

    async def delete_run(request, identity):
        tenant_id = request.query_params.get("tenant_id")
        if not tenant_id:
            raise CoreError("INVALID_REQUEST")
        result = agent.delete_run_data(
            tenant_id,
            request.path_params["run_id"],
            operator_principal_id=identity.principal_id,
        )
        return JSONResponse(result)

    async def delete_run_endpoint(request):
        return await guarded(request, delete_run)

    return [
        Route(
            "/internal/approvals",
            list_endpoint,
            methods=["GET"],
        ),
        Route(
            "/internal/approvals/{approval_id}",
            get_endpoint,
            methods=["GET"],
        ),
        Route(
            "/internal/approvals/{approval_id}:approve",
            approve_endpoint,
            methods=["POST"],
        ),
        Route(
            "/internal/approvals/{approval_id}:deny",
            deny_endpoint,
            methods=["POST"],
        ),
        Route(
            "/internal/runs/{run_id}",
            delete_run_endpoint,
            methods=["DELETE"],
        ),
    ]
