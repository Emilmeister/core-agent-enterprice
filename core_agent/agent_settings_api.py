"""Owner-only profile/MCP configuration and fixed-provider model discovery."""

import asyncio
import json
from urllib.parse import urlunsplit

import httpx
from starlette.responses import JSONResponse
from starlette.routing import Route

from .agent_settings import CONFIG_FIELDS, valid_model_id
from .errors import CoreError
from .model import CompatibleHttpModel
from .owner_api import read_payload, require_owner
from .security import validate_http_url


async def provider_models(model):
    if not isinstance(model, CompatibleHttpModel):
        raise CoreError("MODEL_DISCOVERY_UNAVAILABLE")
    try:
        endpoint = validate_http_url(model.endpoint, "LLM_ENDPOINT")
        suffix = "/chat/completions" if model.api_format == "openai" else "/messages"
        if not endpoint.path.endswith(suffix):
            raise ValueError()
        target = urlunsplit((endpoint.scheme, endpoint.netloc, endpoint.path[:-len(suffix)] + "/models", endpoint.query, ""))
        headers = dict(model.headers)
        if model.api_format == "openai" and model.api_key:
            headers.setdefault("Authorization", "Bearer " + model.api_key)
        if model.api_format == "anthropic":
            if model.api_key:
                headers.setdefault("x-api-key", model.api_key)
            headers.setdefault("anthropic-version", model.anthropic_version)
        async with asyncio.timeout(10), httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", target, headers=headers) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 1048576:
                        raise ValueError()
        payload = json.loads(content)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or len(payload["data"]) > 1000:
            raise ValueError()
        secrets = [value for value in [model.api_key, *model.headers.values()] if isinstance(value, str) and value]
        models = sorted({item["id"] for item in payload["data"] if isinstance(item, dict)
                         and valid_model_id(item.get("id")) and not any(secret in item["id"] for secret in secrets)})
        if not models or payload.get("has_more") is True or payload.get("next_page"):
            raise ValueError()
        return models
    except (CoreError, httpx.HTTPError, ValueError, TypeError, RecursionError, UnicodeError, TimeoutError):
        raise CoreError("MODEL_DISCOVERY_UNAVAILABLE") from None


def agent_settings_routes(agent, store):
    async def endpoint(request):
        try:
            actor = require_owner(request.scope.get("principal"))
            if request.query_params:
                raise CoreError("REQUEST_INVALID")
            if request.method == "GET":
                async for chunk in request.stream():
                    if chunk:
                        raise CoreError("REQUEST_INVALID")
                row = await asyncio.to_thread(store.get, actor.tenant)
                result = store.public(row, agent)
                if request.url.path.endswith("/models"):
                    models = await provider_models(agent.model)
                    result = {"models": models, "current_model_id": result["model_id"]}
            else:
                payload = await read_payload(request, {"expected_revision"}, optional_fields=CONFIG_FIELDS)
                values = {key: value for key, value in payload.items() if key != "expected_revision"}
                if "model_id" in values and values["model_id"] is not None:
                    if not valid_model_id(values["model_id"]):
                        raise CoreError("SETTINGS_INVALID")
                    models = await provider_models(agent.model)
                    if values["model_id"] not in models:
                        raise CoreError("SETTINGS_INVALID")
                row = await asyncio.to_thread(store.update, actor.tenant, values,
                    expected_revision=payload["expected_revision"], actor_id=actor.actor_id)
                result = store.public(row, agent)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except CoreError as error:
            status = {"ACCESS_DENIED": 403, "SETTINGS_CONFLICT": 409,
                      "SETTINGS_CREDENTIAL_UNAVAILABLE": 503, "MODEL_DISCOVERY_UNAVAILABLE": 503}.get(error.code, 400)
            return JSONResponse({"error": {"code": error.code}}, status_code=status, headers={"Cache-Control": "no-store"})
    return [Route("/api/agent-settings", endpoint, methods=["GET", "PUT"]),
            Route("/api/agent-settings/models", endpoint, methods=["GET"])]
