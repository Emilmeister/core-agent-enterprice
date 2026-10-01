"""One bounded network step for a claimed, persisted remote A2A operation."""

from .errors import CoreError
from .remote_agents import RemoteAgentCard, RemoteAgentConnection, _trusted_headers
from .security import redact
from .tasks import REMOTE_TASK_PENDING


class RemoteA2AExecutor:
    def __init__(self, scheduler, registry):
        self.scheduler = scheduler
        self.registry = registry

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

    def _commit(self, claim, current, checkpoint, outcome=None, progress=None):
        return self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                                                  checkpoint=checkpoint, outcome=outcome, progress=progress)

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
            self._commit(claim, current, {**checkpoint, "send_started": True})
            current = self.scheduler.read_remote_claim(claim)
            if current["cancel_requested"]:
                self._finish(claim, current, "canceled", {"reason": "not_dispatched"})
                return
            method = "send_task"
            arguments = {"task": current["contract"]["task"], "message_id": current["contract"]["message_id"]}
        else:
            method = "get_task"
            arguments = {"task_id": checkpoint["remote_task_id"]}
            if current["cancel_requested"]:
                self._commit(claim, current, {**checkpoint, "cancel_started": True, "next_poll_at": None})
                current = self.scheduler.read_remote_claim(claim)
                method = "cancel_task"

        timeout = min(30.0, current["checkpoint"]["deadline"] - current["now"])
        try:
            event = getattr(connection, method)(**arguments, headers=headers, timeout=timeout)
        except Exception as error:
            # No exception text can cross the trust boundary or include private headers.
            current = self.scheduler.read_remote_claim(claim)
            if method != "get_task":
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
        if event.has_files:
            self._commit(claim, current, checkpoint, ("failed", {
                "agent_name": current["contract"]["peer_name"], "reason": "files_unsupported",
            }, "REMOTE_FILES_UNSUPPORTED"))
            return
        if any("text" not in part for part in event.parts):
            self._commit(claim, current, checkpoint, ("failed", {
                "agent_name": current["contract"]["peer_name"], "reason": "parts_unsupported",
            }, "REMOTE_PARTS_UNSUPPORTED"))
            return

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
            self._commit(claim, current, checkpoint, (terminal[0], result, terminal[1]))
        elif method == "cancel_task":
            # A non-terminal cancel response has not confirmed the mutation. Do not repeat it.
            self._unknown(claim, current)
        else:
            self._poll_later(claim, current, checkpoint, progress={"agent_name": current["contract"]["peer_name"], "remote_state": event.state})

    def _poll_later(self, claim, current, checkpoint=None, progress=None):
        checkpoint = current["checkpoint"] if checkpoint is None else checkpoint
        next_poll = min(current["now"] + current["contract"]["poll_interval_seconds"], checkpoint["deadline"])
        self._commit(claim, current, {**checkpoint, "next_poll_at": next_poll}, progress=progress)
