"""Company configuration revisions; plaintext credentials stay in trusted transport."""

import copy
import json
import re
import threading
from contextlib import nullcontext
from dataclasses import replace

from cryptography.fernet import Fernet, InvalidToken
from psycopg.types.json import Jsonb

from .errors import CoreError
from .mcp import RESERVED_MCP_HEADERS
from .remote_agents import _validate_endpoint
from .remote_registry import _header_value, _text
from .security import safe_url


CONFIG_FIELDS = frozenset({"profile_prompt", "model_id", "mcp_servers"})
RESERVED_HEADERS = RESERVED_MCP_HEADERS | frozenset({
    "host", "content-length", "connection", "transfer-encoding", "upgrade", "trailer", "te",
    "proxy-authorization", "traceparent", "tracestate", "baggage",
})


def valid_model_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", value) is not None


def _revision(value):
    if type(value) is not int or not 0 <= value < 2**63 - 1:
        raise CoreError("SETTINGS_INVALID")


def _default(tenant):
    return {"tenant_id": tenant, "revision": 0, "storage_version": 1,
            "config": {"profile_prompt": None, "model_id": None, "mcp_servers": None}, "encrypted_payload": None}


class AgentSettingsStore:
    """The memory adapter shares the workflow lock; PostgreSQL borrows admission's connection."""

    def __init__(self, workflow_store, encryption_key=None, *, database=None, deployment_mcp=()):
        self.database = database
        self._deployment_mcp = tuple(copy.deepcopy(deployment_mcp))
        self._lock = getattr(workflow_store, "_lock", threading.RLock())
        self._current = {}
        self._revisions = {}
        try:
            self._cipher = Fernet(encryption_key.encode("ascii") if isinstance(encryption_key, str) else encryption_key) if encryption_key else (
                None if database else Fernet(Fernet.generate_key()))
        except (ValueError, TypeError, UnicodeError):
            raise CoreError("SETTINGS_CREDENTIAL_UNAVAILABLE") from None

    def get(self, tenant, revision=None, *, connection=None):
        if revision is not None:
            _revision(revision)
        if self.database is None:
            with self._lock:
                revision = self._current.get(tenant, 0) if revision is None else revision
                row = self._revisions.get((tenant, revision)) if revision else _default(tenant)
                if row is None:
                    raise CoreError("CHECKPOINT_INVALID")
                return copy.deepcopy(row)
        with self.database.pool.connection() if connection is None else nullcontext(connection) as connection:
            if revision is None:
                current = connection.execute("SELECT revision FROM core_agent_settings WHERE tenant_id=%s", (tenant,)).fetchone()
                revision = current["revision"] if current else 0
            if revision == 0:
                return _default(tenant)
            row = connection.execute("SELECT * FROM core_agent_setting_revisions WHERE tenant_id=%s AND revision=%s",
                                     (tenant, revision)).fetchone()
            if row is None or row["storage_version"] != 1:
                raise CoreError("CHECKPOINT_INVALID")
            return row

    def _secrets(self, row):
        if row["encrypted_payload"] is None:
            return {}
        if self._cipher is None:
            raise CoreError("SETTINGS_CREDENTIAL_UNAVAILABLE")
        try:
            envelope = json.loads(self._cipher.decrypt(bytes(row["encrypted_payload"])))
            if (set(envelope) != {"version", "tenant_id", "revision", "headers"} or envelope["version"] != 1
                    or envelope["tenant_id"] != row["tenant_id"] or envelope["revision"] != row["revision"]
                    or not isinstance(envelope["headers"], dict)):
                raise ValueError()
            servers = {item["name"]: item for item in row["config"]["mcp_servers"] or ()}
            for name, value in envelope["headers"].items():
                if set(value) != {"header_name", "header_value"} or value["header_name"] != servers[name]["header_name"]:
                    raise ValueError()
                _header_value(value["header_value"])
            return envelope["headers"]
        except (InvalidToken, ValueError, TypeError, KeyError, CoreError, UnicodeError):
            raise CoreError("SETTINGS_CREDENTIAL_UNAVAILABLE") from None

    def _updated(self, previous, values, actor_id):
        config = copy.deepcopy(previous["config"])
        secrets = self._secrets(previous)
        if "profile_prompt" in values:
            prompt = values["profile_prompt"]
            if prompt is not None and (not _text(prompt, 65536, empty=True) or "\0" in prompt):
                raise CoreError("SETTINGS_INVALID")
            config["profile_prompt"] = prompt
        if "model_id" in values:
            if values["model_id"] is not None and not valid_model_id(values["model_id"]):
                raise CoreError("SETTINGS_INVALID")
            config["model_id"] = values["model_id"]
        if "mcp_servers" in values:
            supplied = values["mcp_servers"]
            if supplied is not None and (not isinstance(supplied, list) or len(supplied) > 32):
                raise CoreError("SETTINGS_INVALID")
            old = {item["name"]: item for item in config["mcp_servers"] or ()}
            updated, new_secrets = [], {}
            for item in supplied or ():
                required = {"name", "url", "enabled", "header_name"}
                if not isinstance(item, dict) or not required <= item.keys() <= required | {"header_value"}:
                    raise CoreError("SETTINGS_INVALID")
                name, url, header = item["name"], item["url"], item["header_name"]
                if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
                        or any(server["name"] == name for server in updated) or type(item["enabled"]) is not bool
                        or not _text(url, 4096) or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) < 160 for char in url)):
                    raise CoreError("SETTINGS_INVALID")
                inherited = not header and any(original["name"] == name
                    and safe_url(original["transport"]["url"]) == url for original in self._deployment_mcp)
                if not inherited:
                    try:
                        parsed = _validate_endpoint(url)
                        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                                or "?" in url or "#" in url or "\\" in parsed.netloc or parsed.netloc.endswith(":")
                                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
                            raise ValueError()
                    except (CoreError, ValueError):
                        raise CoreError("SETTINGS_INVALID") from None
                if (not isinstance(header, str) or (header and not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", header))
                        or header.lower() in RESERVED_HEADERS):
                    raise CoreError("SETTINGS_INVALID")
                secret = item.get("header_value")
                if "header_value" not in item and name in secrets:
                    if old[name]["url"] != url or old[name]["header_name"] != header:
                        raise CoreError("SETTINGS_INVALID")
                    secret = secrets[name]["header_value"]
                if secret is not None:
                    if not header:
                        raise CoreError("SETTINGS_INVALID")
                    try:
                        _header_value(secret)
                    except CoreError:
                        raise CoreError("SETTINGS_INVALID") from None
                    new_secrets[name] = {"header_name": header, "header_value": secret}
                updated.append({**{key: item[key] for key in required}, "has_header_value": secret is not None})
            config["mcp_servers"] = updated if supplied is not None else None
            secrets = new_secrets
        row = {"tenant_id": previous["tenant_id"], "revision": previous["revision"] + 1,
               "storage_version": 1, "config": config, "encrypted_payload": None, "actor_id": actor_id}
        if secrets:
            if self._cipher is None:
                raise CoreError("SETTINGS_CREDENTIAL_UNAVAILABLE")
            row["encrypted_payload"] = self._cipher.encrypt(json.dumps({"version": 1,
                "tenant_id": row["tenant_id"], "revision": row["revision"], "headers": secrets},
                sort_keys=True, separators=(",", ":")).encode())
        return row

    def update(self, tenant, values, *, expected_revision, actor_id):
        _revision(expected_revision)
        if not isinstance(values, dict) or not values or set(values) - CONFIG_FIELDS:
            raise CoreError("SETTINGS_INVALID")
        if self.database is None:
            with self._lock:
                current = self.get(tenant)
                if current["revision"] != expected_revision:
                    raise CoreError("SETTINGS_CONFLICT")
                row = self._updated(current, values, actor_id)
                self._revisions[(tenant, row["revision"])] = row
                self._current[tenant] = row["revision"]
                return copy.deepcopy(row)
        with self.database.transaction() as connection:
            connection.execute("INSERT INTO core_agent_settings(tenant_id,revision) VALUES(%s,0) ON CONFLICT DO NOTHING", (tenant,))
            current = connection.execute("SELECT revision FROM core_agent_settings WHERE tenant_id=%s FOR UPDATE", (tenant,)).fetchone()
            if current["revision"] != expected_revision:
                raise CoreError("SETTINGS_CONFLICT")
            row = self._updated(self.get(tenant, current["revision"], connection=connection), values, actor_id)
            connection.execute("""INSERT INTO core_agent_setting_revisions
                (tenant_id,revision,storage_version,config,encrypted_payload,actor_id)
                VALUES(%s,%s,%s,%s,%s,%s)""", (tenant, row["revision"], 1, Jsonb(row["config"]), row["encrypted_payload"], actor_id))
            connection.execute("UPDATE core_agent_settings SET revision=%s WHERE tenant_id=%s", (row["revision"], tenant))
            return row

    def public(self, row, agent):
        config = row["config"]
        servers = config["mcp_servers"]
        if servers is None:
            servers = [{"name": item["name"], "url": safe_url(item["transport"]["url"]), "enabled": True,
                        "header_name": "", "has_header_value": False} for item in agent.platform_mcp]
        else:
            servers = [{**item, "has_header_value": item.get("has_header_value", False)} for item in servers]
        return {"revision": row["revision"], "profile_prompt": config["profile_prompt"] if config["profile_prompt"] is not None
                else agent.agent_config.agent.get("profile_prompt", ""), "model_id": config["model_id"] if config["model_id"] is not None
                else agent.agent_config.model["route"], "mcp_servers": servers,
                "inherits": {key: config[key] is None for key in CONFIG_FIELDS}}

    def catalogs(self, tenant, declarations, workflow_store):
        """Reuse bounded durable discovery evidence for the owner policy editor."""
        targets = {item["name"]: item["transport"] for item in declarations if item.get("owner_configured")}
        if not targets:
            return {}
        if self.database is None:
            with self._lock:
                records = sorted((record for record in workflow_store._records.values()
                    if record.tenant_id == tenant and record.parent_run_id is None and "mcp_catalogs" in record.snapshot),
                    key=lambda record: record.updated_at, reverse=True)[:32]
                snapshots = [copy.deepcopy(record.snapshot) for record in records]
        else:
            with self.database.pool.connection() as connection:
                rows = connection.execute("""SELECT snapshot FROM core_runs WHERE tenant_id=%s
                    AND parent_run_id IS NULL AND snapshot ? 'mcp_catalogs' ORDER BY updated_at DESC LIMIT 32""", (tenant,)).fetchall()
                snapshots = [row["snapshot"] for row in rows]
        catalogs = {}
        for snapshot in snapshots:
            for item in snapshot.get("admission", {}).get("mcp", ()):
                name = item.get("name")
                if name in targets and item.get("owner_configured") and item.get("transport") == targets[name] and name not in catalogs:
                    catalogs[name] = snapshot.get("mcp_catalogs", {}).get(name, {})
        return catalogs

    def configure(self, row, raw, platform, declarations, *, profile=True, credentials=False):
        """Apply owner defaults to roots; child configuration already holds its exact subset."""
        raw = copy.deepcopy(raw)
        config = row["config"]
        if profile:
            if config["profile_prompt"] is not None:
                raw["agent"]["profile_prompt"] = config["profile_prompt"]
            if config["model_id"] is not None:
                raw["model"]["route"] = config["model_id"]
        if config["mcp_servers"] is None:
            return raw, platform, tuple(copy.deepcopy(declarations))
        originals = {item["name"]: item for item in declarations}
        selected = []
        secrets = self._secrets(row) if credentials else {}
        for server in config["mcp_servers"]:
            if not server["enabled"]:
                continue
            original = originals.get(server["name"])
            inherited = (original is not None and safe_url(original["transport"]["url"]) == server["url"]
                         and not server["header_name"])
            declaration = copy.deepcopy(original) if inherited else {
                "name": server["name"], "transport": {"type": "streamable_http", "url": server["url"]},
                "required": False, "owner_configured": True,
            }
            if credentials and not inherited:
                secret = secrets.get(server["name"])
                declaration["headers"] = {secret["header_name"]: secret["header_value"]} if secret else {}
            selected.append(declaration)
        platform = replace(platform, allowed_mcp_servers=platform.allowed_mcp_servers | {item["name"] for item in selected})
        if profile:
            raw["tools"]["mcp"]["allow_servers"] = [item["name"] for item in selected]
            raw["tools"]["mcp"]["owner_servers"] = [item["name"] for item in selected if item.get("owner_configured")]
        return raw, platform, tuple(selected)
