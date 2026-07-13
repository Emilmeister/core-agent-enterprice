from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import socket
import time
import uuid
from urllib.parse import urlparse

import httpx
from a2a.server.owner_resolver import resolve_user_scope
from a2a.server.tasks import PushNotificationConfigStore, PushNotificationSender
from a2a.types import TaskPushNotificationConfig
from a2a.utils.proto_utils import to_stream_response
from cryptography.fernet import Fernet, InvalidToken
from google.protobuf.json_format import MessageToDict, MessageToJson, Parse
from psycopg.types.json import Jsonb

from .errors import CoreError


def _scope(context):
    owner = resolve_user_scope(context)
    owner = owner if context.user.is_authenticated and owner else "anonymous"
    return owner, context.tenant or ""


def _public_addresses(hostname):
    if hostname.lower() == "localhost":
        raise CoreError("PUSH_TARGET_DENIED")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, 443)}
    except socket.gaierror:
        raise CoreError("PUSH_TARGET_UNRESOLVED") from None
    if not addresses:
        raise CoreError("PUSH_TARGET_UNRESOLVED")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise CoreError("PUSH_TARGET_DENIED")
    return addresses


def validate_push_url(url):
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise CoreError("PUSH_TARGET_DENIED")
    _public_addresses(parsed.hostname)
    return parsed.hostname.lower()


class PostgresPushNotificationConfigStore(PushNotificationConfigStore):
    def __init__(self, database, encryption_key):
        self.database = database
        try:
            self._cipher = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
        except (TypeError, ValueError):
            raise CoreError("PUSH_ENCRYPTION_KEY_INVALID") from None

    def _serialize(self, config):
        return self._cipher.encrypt(MessageToJson(config).encode())

    def _deserialize(self, payload):
        try:
            value = self._cipher.decrypt(bytes(payload)).decode()
        except InvalidToken:
            raise CoreError("PUSH_CONFIG_DECRYPTION_FAILED") from None
        return Parse(value, TaskPushNotificationConfig())

    def _set_info(self, task_id, config, context):
        owner, tenant = _scope(context)
        if config.task_id and config.task_id != task_id:
            raise CoreError("INVALID_REQUEST")
        validate_push_url(config.url)
        stored = TaskPushNotificationConfig()
        stored.CopyFrom(config)
        stored.task_id = task_id
        stored.id = stored.id or str(uuid.uuid4())
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO core_push_notification_configs
                   (task_id, config_id, owner, tenant_id, encrypted_payload, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (task_id, config_id, owner, tenant_id) DO UPDATE SET
                     encrypted_payload = EXCLUDED.encrypted_payload,
                     updated_at = EXCLUDED.updated_at""",
                (task_id, stored.id, owner, tenant, self._serialize(stored), time.time()),
            )
        config.CopyFrom(stored)

    def _get_info(self, task_id, context):
        owner, tenant = _scope(context)
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                """SELECT encrypted_payload FROM core_push_notification_configs
                   WHERE task_id = %s AND owner = %s AND tenant_id = %s
                   ORDER BY config_id""",
                (task_id, owner, tenant),
            ).fetchall()
        return [self._deserialize(row["encrypted_payload"]) for row in rows]

    def _get_for_dispatch(self, task_id):
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                """SELECT encrypted_payload FROM core_push_notification_configs
                   WHERE task_id = %s ORDER BY tenant_id, owner, config_id""",
                (task_id,),
            ).fetchall()
        return [self._deserialize(row["encrypted_payload"]) for row in rows]

    def _delete_info(self, task_id, context, config_id):
        owner, tenant = _scope(context)
        sql = (
            "DELETE FROM core_push_notification_configs "
            "WHERE task_id = %s AND owner = %s AND tenant_id = %s"
        )
        values = [task_id, owner, tenant]
        if config_id:
            sql += " AND config_id = %s"
            values.append(config_id)
        with self.database.transaction() as connection:
            connection.execute(sql, values)

    async def set_info(self, task_id, notification_config, context):
        await asyncio.to_thread(self._set_info, task_id, notification_config, context)

    async def get_info(self, task_id, context):
        return await asyncio.to_thread(self._get_info, task_id, context)

    async def get_info_for_dispatch(self, task_id):
        return await asyncio.to_thread(self._get_for_dispatch, task_id)

    async def delete_info(self, task_id, context, config_id=None):
        await asyncio.to_thread(self._delete_info, task_id, context, config_id)


class DurablePushNotificationSender(PushNotificationSender):
    def __init__(
        self,
        database,
        config_store,
        *,
        client=None,
        retry_seconds=1.0,
        telemetry=None,
    ):
        self.database = database
        self.config_store = config_store
        self.client = client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(10, connect=5),
        )
        self._owns_client = client is None
        self.retry_seconds = retry_seconds
        self.telemetry = telemetry

    @staticmethod
    def _event(event):
        encoded = event.SerializeToString(deterministic=True)
        key = f"{type(event).__name__}:{hashlib.sha256(encoded).hexdigest()}"
        payload = MessageToDict(
            to_stream_response(event), preserving_proto_field_name=True
        )
        return key, payload

    def _enqueue(self, task_id, configs, event_key, payload):
        now = time.time()
        with self.database.transaction() as connection:
            for config in configs:
                delivery_id = hashlib.sha256(
                    f"{task_id}\0{config.id}\0{event_key}".encode()
                ).hexdigest()
                connection.execute(
                    """INSERT INTO core_push_deliveries
                       (id, task_id, config_id, event_key, payload, state, attempts,
                        available_at, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, 'pending', 0, %s, %s, %s)
                       ON CONFLICT (task_id, config_id, event_key) DO NOTHING""",
                    (
                        delivery_id,
                        task_id,
                        config.id,
                        event_key,
                        Jsonb(payload),
                        now,
                        now,
                        now,
                    ),
                )

    def _claim(self, limit):
        now = time.time()
        with self.database.transaction() as connection:
            rows = connection.execute(
                """SELECT id, task_id, config_id, payload, attempts
                   FROM core_push_deliveries
                   WHERE state != 'delivered' AND available_at <= %s
                     AND (locked_until IS NULL OR locked_until < %s)
                   ORDER BY available_at, created_at
                   FOR UPDATE SKIP LOCKED LIMIT %s""",
                (now, now, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """UPDATE core_push_deliveries
                       SET state = 'delivering', locked_until = %s, updated_at = %s
                       WHERE id = %s""",
                    (now + 30, now, row["id"]),
                )
        return rows

    def _finish(self, delivery_id, *, delivered, attempts, error_code=None):
        now = time.time()
        delay = min(300.0, self.retry_seconds * (2 ** min(attempts, 8)))
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE core_push_deliveries SET
                     state = %s, attempts = %s, available_at = %s,
                     locked_until = NULL, last_error_code = %s, updated_at = %s,
                     delivered_at = CASE WHEN %s THEN %s ELSE delivered_at END
                   WHERE id = %s""",
                (
                    "delivered" if delivered else "pending",
                    attempts,
                    now if delivered else now + delay,
                    error_code,
                    now,
                    delivered,
                    now,
                    delivery_id,
                ),
            )

    async def _config(self, task_id, config_id):
        configs = await self.config_store.get_info_for_dispatch(task_id)
        return next((item for item in configs if item.id == config_id), None)

    async def send_notification(self, task_id, event):
        configs = await self.config_store.get_info_for_dispatch(task_id)
        if not configs:
            return
        event_key, payload = self._event(event)
        await asyncio.to_thread(self._enqueue, task_id, configs, event_key, payload)
        await self.dispatch_pending()

    async def dispatch_pending(self, *, limit=50):
        rows = await asyncio.to_thread(self._claim, limit)
        for row in rows:
            attempts = row["attempts"] + 1
            span = (
                self.telemetry.span(
                    "core_agent.notification.deliver",
                    attributes={"core_agent.notification.attempt": attempts},
                )
                if self.telemetry
                else contextlib.nullcontext()
            )
            with span:
                config = await self._config(row["task_id"], row["config_id"])
                if config is None:
                    await asyncio.to_thread(
                        self._finish,
                        row["id"],
                        delivered=False,
                        attempts=attempts,
                        error_code="PUSH_CONFIG_NOT_FOUND",
                    )
                    continue
                try:
                    await asyncio.to_thread(validate_push_url, config.url)
                    headers = {
                        "X-Core-Delivery-Id": row["id"],
                        **(
                            {"X-A2A-Notification-Token": config.token}
                            if config.token
                            else {}
                        ),
                    }
                    response = await self.client.post(
                        config.url, json=row["payload"], headers=headers
                    )
                    response.raise_for_status()
                except Exception as error:
                    code = (
                        error.code
                        if isinstance(error, CoreError)
                        else "PUSH_DELIVERY_FAILED"
                    )
                    await asyncio.to_thread(
                        self._finish,
                        row["id"],
                        delivered=False,
                        attempts=attempts,
                        error_code=code,
                    )
                else:
                    await asyncio.to_thread(
                        self._finish,
                        row["id"],
                        delivered=True,
                        attempts=attempts,
                    )
        return len(rows)

    async def run(self, stop_event, *, interval=1.0):
        while not stop_event.is_set():
            await self.dispatch_pending()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def close(self):
        if self._owns_client:
            await self.client.aclose()
