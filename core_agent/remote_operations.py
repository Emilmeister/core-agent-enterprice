"""One bounded network step for a claimed, persisted remote A2A operation."""

import base64
import hashlib
import json

from .errors import CoreError, ExecutionNotStarted
from .remote_agents import RemoteAgentCard, RemoteAgentConnection, _trusted_headers, _validate_response_files
from .security import redact
from .peer_conversations import public_identity
from .tasks import REMOTE_PROGRESS_STATES, REMOTE_TASK_PENDING
from .workspace import WorkspaceBinding


class RemoteA2AExecutor:
    def __init__(self, scheduler, registry, response_files_service=None, chat_file_service=None):
        self.scheduler = scheduler
        self.registry = registry
        self.response_files_service = response_files_service
        self.chat_file_service = chat_file_service

    def __call__(self, claim, cancel_event):
        # Only persisted caller intent authorizes cancellation; shutdown events do not.
        try:
            self._step(claim)
        except Exception as error:
            code = error.code if isinstance(error, CoreError) else "REMOTE_AGENT_UNAVAILABLE"
            if code not in {"LEASE_LOST", "WORKER_STOPPED"}:
                try:
                    current = self.scheduler.read_remote_claim(claim)
                    if code == "CANCEL_REQUESTED" and not current["checkpoint"]["send_started"]:
                        self._finish(claim, current, "canceled", {"reason": "not_dispatched"})
                    elif current["checkpoint"]["send_started"] and current["checkpoint"]["remote_task_id"] is None:
                        self._unknown(claim, current)
                    else:
                        self._finish(claim, current, "failed", {"reason": code.lower()}, code)
                except Exception:
                    # Storage/claim failure leaves recovery to the saved dispatch marker.
                    # Never expose an adapter/database exception containing credentials.
                    pass
        return REMOTE_TASK_PENDING

    def _commit(self, claim, current, checkpoint, outcome=None, progress=None, prepared_file_batch=None, conversation_observation=None):
        return self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                                                  checkpoint=checkpoint, outcome=outcome, progress=progress,
                                                  conversation_observation=conversation_observation,
                                                  **({"prepared_file_batch": prepared_file_batch} if prepared_file_batch is not None else {}))

    def _finish(self, claim, current, state, result, error_code=None):
        result = {"agent_name": current["contract"]["peer_name"], **result}
        return self._commit(claim, current, current["checkpoint"], (state, result, error_code))

    def _unknown(self, claim, current):
        return self._finish(claim, current, "failed", {
            "reason": "reconciliation_required", "remote_outcome": "unknown",
            "message": "Исход внешнего вызова неизвестен. Перед повторной отправкой требуется сверка.",
        }, "SIDE_EFFECT_UNKNOWN")

    def _connection(self, contract):
        peer = self.registry.get_revision(contract["tenant_id"], contract["peer_id"], contract["peer_revision"])
        if (not peer["enabled"] or any(peer[key] != contract[field] for key, field in (
                ("id", "peer_id"), ("revision", "peer_revision"), ("name", "peer_name"), ("url", "url")))):
            raise CoreError("REMOTE_AGENT_DENIED")
        headers = _trusted_headers(self.registry.resolve_headers(
            contract["tenant_id"], contract["peer_id"], contract["peer_revision"]))
        card = RemoteAgentCard(peer["name"], peer["description"], contract["url"], False, (), contract["binding"])
        return RemoteAgentConnection(card, timeout=30), headers

    def _secrets(self, headers):
        values = list(headers.values())
        return values + [value[7:] for value in values if value.lower().startswith("bearer ")]

    def _check_identifiers(self, identifiers, headers):
        if any(secret in identifier for identifier in identifiers if identifier is not None
               for secret in self._secrets(headers) if secret):
            # Identifiers cannot be redacted or rewritten: reject before durable storage or URL construction.
            raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR")

    def _outgoing_files(self, contract):
        if contract["version"] != 2 or not contract["outgoing_files"]:
            return ()
        if self.response_files_service is None:
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        scope = contract["caller_scope"]
        loaded = self.response_files_service.load(
            WorkspaceBinding(contract["tenant_id"], scope["owner_id"], scope["context_id"]),
            contract["outgoing_files"], task_id=scope["task_id"], run_id=scope["run_id"],
            limit_bytes=contract["attachment_limit_bytes"],
        )
        return tuple({"name": ref["name"], "media_type": ref["media_type"], "raw": content}
                     for ref, content in loaded)

    def _accept_files(self, claim, current, checkpoint, event, result, headers, conversation_observation):
        service = self.chat_file_service
        if service is None or service is not getattr(self.scheduler, "chat_file_service", None):
            raise CoreError("FILE_ADMISSION_TRANSACTION_REQUIRED")
        contract = current["contract"]
        _validate_response_files({"message": {"parts": list(event.parts)}}, direct=False,
                                 limit=contract["attachment_limit_bytes"])
        normalized = {"kind": event.kind, "state": event.state, "text": event.text, "final": event.final,
                      "task_id": event.task_id, "context_id": event.context_id, "parts": list(event.parts)}
        try:
            encoded = json.dumps(normalized, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, UnicodeError):
            raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR") from None
        secrets = tuple(secret for secret in self._secrets(headers) if secret)
        secret_bytes = {secret.encode(encoding) for secret in secrets for encoding in ("utf-8", "latin-1")}

        def reflected(value):
            if isinstance(value, str):
                return any(secret in value for secret in secrets)
            if isinstance(value, dict):
                return any(reflected(key) or reflected(item) for key, item in value.items())
            if isinstance(value, (tuple, list)):
                return any(reflected(item) for item in value)
            return False

        if reflected(normalized):
            raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR")
        files, safe_parts = [], []
        for part in event.parts:
            metadata = {key: value for key, value in part.items() if key != "raw"}
            safe_parts.append(metadata)
            if "raw" in part:
                content = base64.b64decode(part["raw"], validate=True)
                if any(secret in content for secret in secret_bytes):
                    raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR")
                files.append({"raw": content, "name": part.get("filename", ""),
                              "media_type": part.get("mediaType", "application/octet-stream"), "metadata": metadata})
        stage = service.prepare(files, tenant_id=claim.tenant_id, actor_id=contract["caller_scope"]["owner_id"],
            message_id=claim.task_id, request_digest=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            source="remote", request_metadata={**normalized, "parts": safe_parts},
            limit_bytes=contract["attachment_limit_bytes"])
        prepared = {key: stage[key] for key in ("batch_id", "lease_token", "actor_id", "message_id", "request_digest")}
        try:
            return self._commit(claim, current, checkpoint, ("completed", result, None), prepared_file_batch=prepared, conversation_observation=conversation_observation)
        finally:
            # The transaction and every source/job/batch lock have exited before cleanup.
            stored = service.store.get(stage["batch_id"], claim.tenant_id)
            if stored["state"] in {"staging", "rejected"}:
                service.reject(stage["batch_id"], claim.tenant_id, stage["lease_token"])

    def _step(self, claim):
        current = self.scheduler.read_remote_claim(claim)
        checkpoint = current["checkpoint"]
        if current["cancel_requested"] and not checkpoint["send_started"]:
            self._finish(claim, current, "canceled", {"reason": "not_dispatched"})
            return
        if (checkpoint["send_started"] and checkpoint["remote_task_id"] is None or checkpoint["cancel_started"]):
            self._unknown(claim, current)
            return

        connection, headers = self._connection(current["contract"])
        # Credential resolution can take time. Recheck the authoritative clock/claim before intent and I/O.
        current = self.scheduler.read_remote_claim(claim)
        checkpoint = current["checkpoint"]
        self._check_identifiers((checkpoint["remote_task_id"], checkpoint["remote_context_id"]), headers)
        if not checkpoint["send_started"]:
            if current["cancel_requested"]:
                self._finish(claim, current, "canceled", {"reason": "not_dispatched"})
                return
            files = self._outgoing_files(current["contract"])
            self._commit(claim, current, {**checkpoint, "send_started": True})
            current = self.scheduler.read_remote_claim(claim)
            if current["cancel_requested"]:
                self._finish(claim, current, "canceled", {"reason": "not_dispatched"})
                return
            method = "send_task"
            arguments = {"task": current["contract"]["task"], "message_id": current["contract"]["message_id"]}
            if current["contract"]["version"] == 2:
                arguments["files"] = files
        else:
            method = "get_task"
            arguments = {"task_id": checkpoint["remote_task_id"]}
            if current["cancel_requested"]:
                self._commit(claim, current, {**checkpoint, "cancel_started": True, "next_poll_at": None})
                current = self.scheduler.read_remote_claim(claim)
                method = "cancel_task"

        if current["contract"]["version"] == 2:
            arguments["attachment_limit_bytes"] = current["contract"]["attachment_limit_bytes"]

        timeout = min(30.0, current["checkpoint"]["deadline"] - current["now"])
        try:
            event = getattr(connection, method)(**arguments, headers=headers, timeout=timeout)
        except Exception as error:
            # No exception text can cross the trust boundary or include private headers.
            current = self.scheduler.read_remote_claim(claim)
            if isinstance(error, ExecutionNotStarted):
                self._finish(claim, current, "failed", {"reason": error.code.lower()}, error.code)
            elif method != "get_task":
                self._unknown(claim, current)
            elif isinstance(error, CoreError) and error.retryable:
                self._poll_later(claim, current)
            else:
                self._finish(claim, current, "failed", {"reason": "remote_read_failed", "remote_outcome": "unknown"},
                             error.code if isinstance(error, CoreError) else "REMOTE_AGENT_UNAVAILABLE")
            return

        current = self.scheduler.read_remote_claim(claim)
        checkpoint = current["checkpoint"]
        self._check_identifiers((event.task_id, event.context_id), headers)
        if event.task_id is not None:
            for key, value in (("remote_task_id", event.task_id), ("remote_context_id", event.context_id)):
                if checkpoint[key] is not None and checkpoint[key] != value:
                    self._finish(claim, current, "failed", {"reason": "remote_identity_changed", "remote_outcome": "unknown"},
                                 "REMOTE_AGENT_PROTOCOL_ERROR")
                    return
                checkpoint[key] = value
        early_files = (current["contract"]["version"] == 2 and event.kind == "task"
                       and not event.final and event.state in REMOTE_PROGRESS_STATES)
        completed_files = (current["contract"]["version"] == 2 and event.final
                           and (event.kind == "message" or event.kind == "task" and event.state == "TASK_STATE_COMPLETED"))
        if event.has_files and not early_files and not completed_files:
            self._commit(claim, current, checkpoint, ("failed", {
                "agent_name": current["contract"]["peer_name"], "reason": "files_unsupported",
            }, "REMOTE_FILES_UNSUPPORTED"))
            return
        if any("text" not in part and not ((early_files or completed_files) and "raw" in part) for part in event.parts):
            self._commit(claim, current, checkpoint, ("failed", {
                "agent_name": current["contract"]["peer_name"], "reason": "parts_unsupported",
            }, "REMOTE_PARTS_UNSUPPORTED"))
            return

        published = list(event.messages)
        if not event.public_messages_complete and not published and event.text and event.state not in {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}:
            # Legacy adapters supply only the current public snapshot, never reconstructed history.
            published = [{"id": public_identity("snapshot", event.text), "text": event.text}]
        safe_messages = []
        for message in published:
            text = redact(message["text"], known_secrets=self._secrets(headers))
            safe_messages.append({"id": public_identity(message["id"], text), "text": text})
        observation = {"messages": safe_messages, "history_truncated": event.history_truncated}
        result = {"agent_name": current["contract"]["peer_name"], "remote_state": event.state,
                  "text": redact(event.text, known_secrets=self._secrets(headers))}
        terminal = {
            "TASK_STATE_COMPLETED": ("completed", None),
            "TASK_STATE_FAILED": ("failed", "REMOTE_TASK_FAILED"),
            "TASK_STATE_CANCELED": ("canceled", None),
            "TASK_STATE_REJECTED": ("failed", "REMOTE_TASK_REJECTED"),
        }.get(event.state)
        if event.kind == "message":
            terminal = ("completed", None)
        if terminal is not None:
            if completed_files and event.has_files:
                try:
                    self._accept_files(claim, current, checkpoint, event, result, headers, observation)
                except CoreError as error:
                    if error.code in {"LEASE_LOST", "WORKER_STOPPED"}:
                        raise
                    state = "canceled" if error.code == "CANCEL_REQUESTED" else "failed"
                    self._commit(claim, current, checkpoint, (state, {
                        "agent_name": current["contract"]["peer_name"], "reason": error.code.lower()},
                        None if state == "canceled" else error.code))
            else:
                self._commit(claim, current, checkpoint, (terminal[0], result, terminal[1]), conversation_observation=observation)
        elif method == "cancel_task":
            # A non-terminal cancel response has not confirmed the mutation. Do not repeat it.
            self._unknown(claim, current)
        else:
            self._poll_later(claim, current, checkpoint, progress={"agent_name": current["contract"]["peer_name"], "remote_state": event.state}, conversation_observation=observation)

    def _poll_later(self, claim, current, checkpoint=None, progress=None, conversation_observation=None):
        checkpoint = current["checkpoint"] if checkpoint is None else checkpoint
        contract, now = current["contract"], current["now"]
        started_at = checkpoint["deadline"] - contract["timeout_seconds"]
        next_poll = min(now + contract["poll_interval_seconds"], checkpoint["deadline"])
        for phase_end, interval in ((180, 10), (780, 30)):
            boundary = started_at + phase_end
            if now < boundary:
                next_poll = min(next_poll, now + interval, boundary)
                break
        if (current.get("observed_until") or 0) > now:
            next_poll = min(next_poll, now + 15)
        self._commit(claim, current, {**checkpoint, "next_poll_at": next_poll}, progress=progress,
                     conversation_observation=conversation_observation)
