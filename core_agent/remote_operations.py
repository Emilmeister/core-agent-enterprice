"""One bounded network step for a claimed, persisted remote A2A operation."""

import base64
import hashlib
import json
from dataclasses import replace

from .errors import CoreError, ExecutionNotStarted
from .remote_agents import RemoteAgentCard, RemoteAgentConnection, _trusted_headers, _validate_response_files
from .security import redact
from .peer_conversations import public_identity
from .tasks import REMOTE_PROGRESS_STATES, REMOTE_TASK_PENDING
from .workspace import WorkspaceBinding
from .remote_artifacts import prepare_remote_artifacts, complete_artifact, new_remote_artifacts


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
            if code not in {"LEASE_LOST", "WORKER_STOPPED", "SESSION_CONFLICT"}:
                try:
                    current = self.scheduler.read_remote_claim(claim)
                    if code == "CANCEL_REQUESTED" and not current["checkpoint"]["send_started"]:
                        self._finish(claim, current, "canceled", {"reason": "not_dispatched", "remote_outcome": "not_dispatched"})
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

    def _material_failure(self, claim, current, checkpoint, error, event):
        if error.code in {"LEASE_LOST", "WORKER_STOPPED", "SESSION_CONFLICT"}:
            raise error
        checkpoint = {**checkpoint, "version": current["checkpoint"]["version"]}
        for key in ("received_artifacts", "file_batch_ids"):
            if key in current["checkpoint"]:
                checkpoint[key] = current["checkpoint"][key]
            else:
                checkpoint.pop(key, None)
        state = "canceled" if error.code == "CANCEL_REQUESTED" else "failed"
        self._commit(claim, current, checkpoint, (state, {
            "agent_name": current["contract"]["peer_name"], "reason": error.code.lower(),
            "remote_state": event.state, **({"remote_outcome": "completed"} if event.kind == "message" else {})},
            None if state == "canceled" else error.code))

    def _connection(self, contract):
        peer = self.registry.get_revision(contract["tenant_id"], contract["peer_id"], contract["peer_revision"])
        if (not peer["enabled"] or any(peer[key] != contract[field] for key, field in (
                ("id", "peer_id"), ("revision", "peer_revision"), ("name", "peer_name"), ("url", "url")))):
            raise CoreError("REMOTE_AGENT_DENIED")
        if "conversation" in contract and contract["conversation"]["generation"] != self.registry.connection_generation(
                contract["tenant_id"], contract["peer_id"], contract["peer_revision"]):
            raise CoreError("CHECKPOINT_INVALID")
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

    def _outgoing_files(self, contract, refs=None):
        refs = contract.get("outgoing_files", []) if refs is None else refs
        if contract["version"] < 2 or not refs:
            return ()
        if self.response_files_service is None:
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        scope = contract["caller_scope"]
        loaded = self.response_files_service.load(
            WorkspaceBinding(contract["tenant_id"], scope["owner_id"], scope["context_id"]),
            refs, task_id=scope["task_id"], run_id=scope["run_id"],
            limit_bytes=contract["attachment_limit_bytes"],
        )
        return tuple({"name": ref["name"], "media_type": ref["media_type"], "raw": content}
                     for ref, content in loaded)

    def _accept_files(self, claim, current, checkpoint, event, result, headers, conversation_observation, progress=None):
        service = self.chat_file_service
        if service is None or service is not getattr(self.scheduler, "chat_file_service", None):
            raise CoreError("FILE_ADMISSION_TRANSACTION_REQUIRED")
        contract = current["contract"]
        _validate_response_files({"message": {"parts": list(event.parts)}}, direct=False,
                                 limit=contract["attachment_limit_bytes"])
        normalized = {"kind": event.kind, "state": event.state, "text": event.text, "final": event.final,
                      "task_id": event.task_id, "context_id": event.context_id, "parts": list(event.parts)}
        if contract["version"] == 3:
            normalized.update(artifacts=list(event.artifacts), messages=list(event.public_messages),
                              message_parts=list(event.message_parts), message_id=event.message_id)
        digest_material = normalized
        if contract["version"] == 3 and checkpoint["version"] == 3:
            digest_material = {"artifacts": list(event.artifacts),
                "message_parts": [part for part in event.message_parts if "text" not in part]}
            if digest_material["message_parts"]:
                digest_material["message_id"] = event.message_id
        try:
            encoded = json.dumps(digest_material, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
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
        typed_parts = list(event.message_parts) + [part for artifact in event.artifacts for part in artifact.get("parts", ())]
        for part in typed_parts if contract["version"] == 3 else event.parts:
            metadata = {key: value for key, value in part.items() if key != "raw"}
            safe_parts.append(metadata)
            if "raw" in part:
                content = base64.b64decode(part["raw"], validate=True)
                if any(secret in content for secret in secret_bytes):
                    raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR")
                files.append({"raw": content, "name": part.get("filename", ""),
                              "media_type": part.get("mediaType", "application/octet-stream"), "metadata": metadata})
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if contract["version"] == 3:
            immutable = checkpoint["version"] == 3
            destination_id = hashlib.sha256((claim.task_id + ":" + digest).encode()).hexdigest() if immutable else None
            stage = prepare_remote_artifacts(service, event, tenant_id=claim.tenant_id,
                actor_id=contract["caller_scope"]["owner_id"],
                message_id=claim.task_id + ":artifact:" + digest if immutable else claim.task_id,
                request_digest=digest, operation_id=claim.task_id, limit_bytes=contract["attachment_limit_bytes"],
                destination_id=destination_id)
            if stage is None:
                raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR")
        else:
            stage = service.prepare(files, tenant_id=claim.tenant_id, actor_id=contract["caller_scope"]["owner_id"],
                message_id=claim.task_id, request_digest=digest,
                source="remote", request_metadata={**normalized, "parts": safe_parts},
                limit_bytes=contract["attachment_limit_bytes"])
        prepared = {key: stage[key] for key in ("batch_id", "lease_token", "actor_id", "message_id", "request_digest")}
        try:
            outcome = ("completed", result, None) if event.final else None
            return self._commit(claim, current, checkpoint, outcome, progress=progress,
                prepared_file_batch=prepared, conversation_observation=conversation_observation)
        finally:
            # The transaction and every source/job/batch lock have exited before cleanup.
            stored = service.store.get(stage["batch_id"], claim.tenant_id)
            if stored["state"] in {"staging", "rejected"}:
                service.reject(stage["batch_id"], claim.tenant_id, stage["lease_token"])

    def _step(self, claim):
        current = self.scheduler.read_remote_claim(claim)
        checkpoint = current["checkpoint"]
        if current["cancel_requested"] and not checkpoint["send_started"]:
            self._finish(claim, current, "canceled", {"reason": "not_dispatched", "remote_outcome": "not_dispatched"})
            return
        if (checkpoint["send_started"] and checkpoint["remote_task_id"] is None or checkpoint["cancel_started"]):
            self._unknown(claim, current)
            return
        if checkpoint.get("reply") and checkpoint["reply"]["status"] == "started":
            self._unknown(claim, current)
            return

        connection, headers = self._connection(current["contract"])
        # Credential resolution can take time. Recheck the authoritative clock/claim before intent and I/O.
        current = self.scheduler.read_remote_claim(claim)
        checkpoint = current["checkpoint"]
        self._check_identifiers((checkpoint["remote_task_id"], checkpoint["remote_context_id"]), headers)
        if not checkpoint["send_started"]:
            if current["cancel_requested"]:
                self._finish(claim, current, "canceled", {"reason": "not_dispatched", "remote_outcome": "not_dispatched"})
                return
            files = self._outgoing_files(current["contract"])
            self._commit(claim, current, {**checkpoint, "send_started": True})
            current = self.scheduler.read_remote_claim(claim)
            if current["cancel_requested"]:
                self._finish(claim, current, "canceled", {"reason": "not_dispatched", "remote_outcome": "not_dispatched"})
                return
            method = "send_task"
            arguments = {"task": current["contract"]["task"], "message_id": current["contract"]["message_id"]}
            context_id = current["checkpoint"]["remote_context_id"]
            self._check_identifiers((context_id,), headers)
            if context_id is not None:
                arguments["context_id"] = context_id
            if current["contract"]["version"] >= 2:
                arguments["files"] = files
        elif checkpoint.get("reply") and checkpoint["reply"]["status"] == "queued" and not current["cancel_requested"]:
            reply = checkpoint["reply"]
            files = self._outgoing_files(current["contract"], reply["outgoing_files"])
            self._commit(claim, current, {**checkpoint, "reply": {**reply, "status": "started"}})
            current = self.scheduler.read_remote_claim(claim)
            if current["cancel_requested"]:
                self._unknown(claim, current)
                return
            method = "send_task"
            arguments = {"task": reply["task"], "message_id": reply["message_id"], "files": files,
                         "task_id": checkpoint["remote_task_id"], "context_id": checkpoint["remote_context_id"]}
        else:
            method = "get_task"
            arguments = {"task_id": checkpoint["remote_task_id"]}
            if current["cancel_requested"]:
                self._commit(claim, current, {**checkpoint, "cancel_started": True, "next_poll_at": None})
                current = self.scheduler.read_remote_claim(claim)
                method = "cancel_task"

        if current["contract"]["version"] >= 2:
            arguments["attachment_limit_bytes"] = current["contract"]["attachment_limit_bytes"]

        timeout = min(30.0, current["checkpoint"]["deadline"] - current["now"])
        try:
            event = getattr(connection, method)(**arguments, headers=headers, timeout=timeout)
        except Exception as error:
            # No exception text can cross the trust boundary or include private headers.
            current = self.scheduler.read_remote_claim(claim)
            if isinstance(error, ExecutionNotStarted):
                self._finish(claim, current, "failed", {"reason": error.code.lower(),
                    "remote_outcome": "not_dispatched"} if method == "send_task" and not checkpoint.get("reply")
                    else {"reason": error.code.lower(), "remote_outcome": "unknown"}, error.code)
            elif method != "get_task":
                self._unknown(claim, current)
            elif isinstance(error, CoreError) and error.retryable:
                self._poll_later(claim, current)
            else:
                self._finish(claim, current, "failed", {"reason": "remote_read_failed", "remote_outcome": "unknown"},
                             error.code if isinstance(error, CoreError) else "REMOTE_AGENT_UNAVAILABLE")
            return

        current = self.scheduler.read_remote_claim(claim)
        if method == "get_task" and current["checkpoint"].get("reply") != checkpoint.get("reply"):
            # A concurrent safe-boundary reply admission wins over this obsolete read.
            return
        checkpoint = current["checkpoint"]
        self._check_identifiers((event.task_id, event.context_id, event.message_id), headers)
        for key, value in (("remote_task_id", event.task_id), ("remote_context_id", event.context_id)):
            if value is None:
                continue
            if checkpoint[key] is not None and checkpoint[key] != value:
                self._finish(claim, current, "failed", {"reason": "remote_identity_changed", "remote_outcome": "unknown"},
                             "REMOTE_AGENT_PROTOCOL_ERROR")
                return
            checkpoint[key] = value
        version = current["contract"]["version"]
        if version == 3 and not event.public_messages_complete:
            # Compatibility for custom in-process adapters predating the typed parser.
            messages = event.public_messages or event.messages
            if not messages and not event.artifacts and event.text and event.state not in {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}:
                messages = ({"id": public_identity("snapshot", event.text), "text": event.text},)
            event = replace(event, public_messages=messages,
                            message_parts=event.message_parts or (() if event.artifacts else event.parts))
        if version == 3 and (checkpoint["version"] == 3 or any(complete_artifact(artifact) for artifact in event.artifacts)):
            try:
                event, receipts = new_remote_artifacts(event, checkpoint, current["contract"]["attachment_limit_bytes"])
            except CoreError as error:
                self._material_failure(claim, current, checkpoint, error, event)
                return
            if event.artifacts or checkpoint["version"] == 3:
                checkpoint = {**checkpoint, "version": 3, "received_artifacts": receipts,
                              "file_batch_ids": checkpoint.get("file_batch_ids", [])}
        early_files = (version >= 2 and event.kind == "task"
                       and not event.final and event.state in REMOTE_PROGRESS_STATES)
        completed_files = (version >= 2 and event.final
                           and (event.kind == "message" or event.kind == "task" and event.state == "TASK_STATE_COMPLETED"))
        public_parts = list(event.message_parts) + [part for artifact in event.artifacts for part in artifact.get("parts", ())]
        has_files = any("raw" in part or "url" in part for part in public_parts) if version == 3 else event.has_files
        if has_files and not early_files and not completed_files:
            self._commit(claim, current, checkpoint, ("failed", {
                "agent_name": current["contract"]["peer_name"], "reason": "files_unsupported", "remote_state": event.state,
            }, "REMOTE_FILES_UNSUPPORTED"))
            return
        if version != 3 and any("text" not in part and not ((early_files or completed_files) and "raw" in part) for part in event.parts):
            self._commit(claim, current, checkpoint, ("failed", {
                "agent_name": current["contract"]["peer_name"], "reason": "parts_unsupported", "remote_state": event.state,
                **({"remote_outcome": "completed"} if event.kind == "message" else {}),
            }, "REMOTE_PARTS_UNSUPPORTED"))
            return

        published = list(event.public_messages if version == 3 else event.messages)
        if version != 3 and not event.public_messages_complete and not published and event.text and event.state not in {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}:
            # Legacy adapters supply only the current public snapshot, never reconstructed history.
            published = [{"id": public_identity("snapshot", event.text), "text": event.text}]
        safe_messages = []
        for message in published:
            text = redact(message["text"], known_secrets=self._secrets(headers))
            safe_messages.append({"id": public_identity(message.get("id", "snapshot"), text), "text": text})
        if version == 3:
            reply = checkpoint["reply"]
            if reply and reply["status"] == "started" and method == "send_task":
                checkpoint["reply"] = {**reply, "status": "confirmed"}
                checkpoint["answered_inputs"] = [*checkpoint["answered_inputs"], reply["input_id"]]
                checkpoint["input_request"] = None
                safe_messages.append({"id": public_identity("outgoing:" + reply["message_id"], reply["task"]),
                    "text": redact(reply["task"], known_secrets=self._secrets(headers)), "direction": "outgoing",
                    "request_provenance": reply["request_provenance"]})
            pending = event.input_request
            if pending and not event.owner_wait and pending["input_id"] not in checkpoint["answered_inputs"]:
                text = redact(pending["question"], known_secrets=self._secrets(headers))
                pending = {"input_id": pending["input_id"], "question": text}
                if checkpoint["input_request"] is not None and checkpoint["input_request"] != pending:
                    raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR")
                checkpoint["input_request"] = pending
                identity = (public_identity("message:" + event.message_id, event.input_request["question"])
                            if event.message_id else "input:" + pending["input_id"])
                safe_messages.append({"id": public_identity(identity, text), "text": text})
        observation = {"messages": safe_messages, "history_truncated": event.history_truncated}
        result = {"agent_name": current["contract"]["peer_name"], "remote_state": event.state,
                  "text": redact("\n".join(message["text"] for message in event.public_messages)
                                 if version == 3 else event.text, known_secrets=self._secrets(headers))}
        terminal = {
            "TASK_STATE_COMPLETED": ("completed", None),
            "TASK_STATE_FAILED": ("failed", "REMOTE_TASK_FAILED"),
            "TASK_STATE_CANCELED": ("canceled", None),
            "TASK_STATE_REJECTED": ("failed", "REMOTE_TASK_REJECTED"),
        }.get(event.state)
        if event.kind == "message":
            terminal = ("completed", None)
        if terminal is not None:
            typed_material = version == 3 and (any(artifact.get("parts") for artifact in event.artifacts)
                or any("data" in part or "raw" in part for part in event.message_parts))
            if completed_files and (has_files or typed_material):
                try:
                    self._accept_files(claim, current, checkpoint, event, result, headers, observation)
                except CoreError as error:
                    self._material_failure(claim, current, checkpoint, error, event)
            else:
                self._commit(claim, current, checkpoint, (terminal[0], result, terminal[1]), conversation_observation=observation)
        elif method == "cancel_task":
            # A non-terminal cancel response has not confirmed the mutation. Do not repeat it.
            self._unknown(claim, current)
        else:
            progress = {"agent_name": current["contract"]["peer_name"], "remote_state": event.state}
            if version == 3 and checkpoint["input_request"] and (not checkpoint["reply"] or checkpoint["reply"]["status"] == "confirmed"):
                progress.update(checkpoint["input_request"])
            elif version == 3 and event.owner_wait:
                progress["owner_wait"] = True
            if version == 3 and checkpoint["version"] == 3 and event.artifacts:
                checkpoint["next_poll_at"] = self._next_poll_at(current, checkpoint)
                try:
                    self._accept_files(claim, current, checkpoint, event, result, headers, observation, progress=progress)
                except CoreError as error:
                    self._material_failure(claim, current, checkpoint, error, event)
            else:
                self._poll_later(claim, current, checkpoint, progress=progress, conversation_observation=observation)

    @staticmethod
    def _next_poll_at(current, checkpoint):
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
        return next_poll

    def _poll_later(self, claim, current, checkpoint=None, progress=None, conversation_observation=None):
        checkpoint = current["checkpoint"] if checkpoint is None else checkpoint
        self._commit(claim, current, {**checkpoint, "next_poll_at": self._next_poll_at(current, checkpoint)}, progress=progress,
                     conversation_observation=conversation_observation)
