"""Cloud.ru-only A2A 0.3 wire adaptation around the shared SDK handler."""

import json
import logging
import re
from contextlib import aclosing

from a2a.server.request_handlers.response_helpers import build_error_response
# The SDK compat module alone imports routes back through its own dispatcher.
from a2a.server.routes.jsonrpc_dispatcher import JSONRPC03Adapter
from a2a.utils.errors import (
    A2AError,
    InternalError,
    InvalidParamsError,
    InvalidRequestError,
    MethodNotFoundError,
    VersionNotSupportedError,
)
from pydantic import ValidationError
from sse_starlette.sse import EventSourceResponse
from starlette.responses import JSONResponse


logger = logging.getLogger(__name__)


def cloud_a2a_version(request, default="0.3"):
    """Validate the explicit version without echoing untrusted header values."""
    values = request.headers.getlist("a2a-version")
    if not values:
        return default
    match = re.fullmatch(r"(0\.3|1\.0)(?:\.[0-9]+)?", values[0].strip())
    if len(values) != 1 or match is None:
        raise VersionNotSupportedError()
    return match[1]


def _unexpected_error(error):
    logger.warning("Legacy A2A operation failed (%s)", type(error).__name__)
    return InternalError()


class CloudJSONRPC03Adapter(JSONRPC03Adapter):
    """Reuse SDK conversions while keeping validation and errors content-safe."""

    async def handle_request(self, request_id, method, body, request):
        request_id = request_id if type(request_id) in {str, int} else None
        try:
            if (not isinstance(body, dict) or body.get("jsonrpc") != "2.0"
                    or type(body.get("id")) not in {str, int}):
                raise InvalidRequestError()
            if not self.supports_method(method):
                raise MethodNotFoundError()
            if cloud_a2a_version(request) != "0.3":
                raise VersionNotSupportedError()
            try:
                specific_request = self.METHOD_TO_MODEL[method].model_validate(body)
            except ValidationError:
                raise InvalidParamsError() from None

            context = self._context_builder.build(request)
            context.tenant = getattr(getattr(specific_request, "params", None), "tenant", "")
            context.state["method"] = method
            context.state["request_id"] = request_id
            if method in {"message/stream", "tasks/resubscribe"}:
                return await self._process_streaming_request(request_id, specific_request, context)
            return await self._process_non_streaming_request(request_id, specific_request, context)
        except A2AError as error:
            return JSONResponse(build_error_response(request_id, error))
        except Exception as error:
            return JSONResponse(build_error_response(request_id, _unexpected_error(error)))

    async def _process_streaming_request(self, request_id, request_obj, context):
        source = (self.handler.on_message_send_stream(request_obj, context)
                  if request_obj.method == "message/stream"
                  else self.handler.on_subscribe_to_task(request_obj, context))

        async def events():
            try:
                async with aclosing(source):
                    async for item in source:
                        yield {"data": item.model_dump_json(by_alias=True, exclude_none=True)}
            except A2AError as error:
                yield {"data": json.dumps(build_error_response(request_id, error))}
            except Exception as error:
                yield {"data": json.dumps(build_error_response(request_id, _unexpected_error(error)))}

        return EventSourceResponse(events())
