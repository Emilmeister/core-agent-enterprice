"""Bound and validate A2A JSON before the SDK allocates protobuf file bytes."""

import base64
import binascii
import json
import logging
import math
import os
import re

from a2a.server.jsonrpc_models import JSONRPCError
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers.response_helpers import EXCEPTION_MAP, build_error_response
from a2a.utils.error_handlers import build_rest_error_payload
from a2a.utils.errors import (
    A2AError,
    A2A_ERROR_MAPPING,
    A2A_ERROR_REASONS,
    ContentTypeNotSupportedError,
    ErrorMapping,
    InvalidParamsError,
    JSON_RPC_ERROR_CODE_MAP,
)
from google.protobuf.message import Message as ProtoMessage
from starlette.responses import JSONResponse

from .errors import CoreError


class RequestTooLargeError(A2AError):
    message = "A2A request exceeds the deployment transport limit"


class InvalidJsonInputError(A2AError):
    message = "Invalid JSON request"


for error, status, grpc, reason, rpc_code in (
    (RequestTooLargeError, 413, "RESOURCE_EXHAUSTED", "REQUEST_TOO_LARGE", -32602),
    (InvalidJsonInputError, 400, "INVALID_ARGUMENT", "REQUEST_INVALID", -32700),
):
    A2A_ERROR_MAPPING[error] = ErrorMapping(status, grpc, reason)
    A2A_ERROR_REASONS[error] = reason
    JSON_RPC_ERROR_CODE_MAP[error] = rpc_code
    EXCEPTION_MAP[error] = JSONRPCError


def request_limit():
    try:
        value = int(os.environ.get("A2A_MAX_REQUEST_BYTES", "").strip() or "40000000")
    except ValueError:
        raise CoreError("CONFIG_INVALID", "A2A_MAX_REQUEST_BYTES must be an integer from 524288 to 2147483647") from None
    if not 524288 <= value <= 2147483647:
        raise CoreError("CONFIG_INVALID", "A2A_MAX_REQUEST_BYTES must be an integer from 524288 to 2147483647")
    return value


def _omit_sdk_payload(record):
    # The pinned SDK logs complete request dicts and protobuf Tasks/events at DEBUG.
    args = record.args.values() if isinstance(record.args, dict) else record.args
    if record.levelno == logging.DEBUG and (
        record.msg in {"Request body: %s", "Dequeued event: %s"}
        or any(isinstance(arg, (ProtoMessage, ServerCallContext)) for arg in args)
    ):
        return False
    if record.exc_info:
        record.msg, record.args = "SDK operation failed (%s)", (record.exc_info[0].__name__,)
        record.exc_info = record.exc_text = None
    elif record.msg == "Parse error: %s":
        record.args = ("invalid request",)
    elif isinstance(record.msg, str) and record.msg.startswith("Request Error (ID:"):
        record.msg, record.args = "SDK request error: code=%s", (record.args[1],)
    elif isinstance(record.msg, str) and record.msg.startswith("Request error: Code="):
        record.msg, record.args = "SDK request error: code=%s", (record.args[0],)
    elif isinstance(record.args, tuple):
        record.args = tuple(type(arg).__name__ if isinstance(arg, BaseException) else arg for arg in record.args)
    return True


def protect_sdk_logs():
    for name in (
        "a2a.server.routes.jsonrpc_dispatcher",
        "a2a.server.routes.rest_dispatcher",
        "a2a.server.request_handlers.default_request_handler_v2",
        "a2a.server.agent_execution.active_task",
        "a2a.server.events.event_queue_v2",
        "a2a.utils.error_handlers",
    ):
        logging.getLogger(name).addFilter(_omit_sdk_payload)


def _unique_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate JSON member")
    return value


def _invalid_constant(_value):
    raise ValueError("nonfinite JSON number")


def _check_depth(payload):
    pending = [iter((payload,))]
    while pending:
        try:
            value = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("nonfinite JSON number")
        if isinstance(value, (dict, list)):
            if len(pending) > 100:  # Same ceiling as the SDK's protobuf JSON parser.
                raise ValueError("JSON nesting limit")
            children = value.values() if isinstance(value, dict) else value
            pending.append(iter(children))


def _validate_files(message):
    if not isinstance(message, dict) or not isinstance(message.get("parts"), list):
        return  # The SDK retains responsibility for the public message schema.
    for part in message["parts"]:
        if not isinstance(part, dict) or "raw" not in part:
            continue
        raw = part["raw"]
        if not isinstance(raw, str) or len(raw) % 4 or not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", raw):
            raise InvalidParamsError("File bytes require canonical standard base64", data={"code": "INVALID_FILE_ENCODING"})
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error):
            raise InvalidParamsError("File bytes require canonical standard base64", data={"code": "INVALID_FILE_ENCODING"}) from None
        if base64.b64encode(decoded).decode("ascii") != raw:
            raise InvalidParamsError("File bytes require canonical standard base64", data={"code": "INVALID_FILE_ENCODING"})


async def read_input(request, limit, *, rpc):
    encodings = request.headers.getlist("content-encoding")
    if any(encoding.strip().lower() != "identity" for encoding in encodings):
        raise ContentTypeNotSupportedError(data={"code": "CONTENT_TYPE_NOT_SUPPORTED"})
    lengths = request.headers.getlist("content-length")
    if lengths:
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,20}", lengths[0]):
            raise InvalidParamsError("Invalid Content-Length", data={"code": "REQUEST_INVALID"})
        declared = int(lengths[0])
        if declared > limit:
            raise RequestTooLargeError(data={"code": "REQUEST_TOO_LARGE", "allowed_bytes": str(limit), "actual_bytes": str(declared)})
    body = bytearray()
    async for chunk in request.stream():
        size = len(body) + len(chunk)
        if size > limit:
            raise RequestTooLargeError(data={"code": "REQUEST_TOO_LARGE", "allowed_bytes": str(limit), "actual_bytes": str(size)})
        body.extend(chunk)
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        _check_depth(payload)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise InvalidJsonInputError(data={"code": "REQUEST_INVALID"}) from None
    if rpc:
        envelopes = payload if isinstance(payload, list) else [payload]
        for envelope in envelopes:
            if (isinstance(envelope, dict) and isinstance(envelope.get("method"), str)
                    and envelope["method"] in {"SendMessage", "SendStreamingMessage"}):
                params = envelope.get("params")
                if isinstance(params, dict):
                    try:
                        _validate_files(params.get("message"))
                    except A2AError as error:
                        request_id = envelope.get("id")
                        error.request_id = request_id if type(request_id) in {str, int} else None
                        raise
    elif isinstance(payload, dict):
        _validate_files(payload.get("message"))
    return bytes(body), payload


def input_error_response(error, *, rpc, request_id=None):
    if rpc:
        return JSONResponse(build_error_response(request_id, error))
    mapping = A2A_ERROR_MAPPING[type(error)]
    return JSONResponse(build_rest_error_payload(error), status_code=mapping.http_code)
