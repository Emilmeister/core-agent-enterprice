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
from .mcp_credentials import (
    certificate_info as public_certificate_info,
    normalize_client_certificate,
    validate_proxy_credentials,
    validate_proxy_url,
    validate_stored_client_certificate,
    validate_stored_proxy_credentials,
)
from .remote_agents import _validate_endpoint
from .remote_registry import _header_value, _text
from .security import safe_url
from .structured_output import STRUCTURED_OUTPUT_DEFAULT, validate_structured_output


CONFIG_FIELDS = frozenset({"profile_prompt", "model_id", "mcp_servers", "context_window", "structured_output", "guardrails_enabled", "next_steps"})
# Read-only display fields: a client may echo them back, and the server derives them again.
DERIVED_FIELDS = frozenset({"has_header_value", "has_proxy_credentials",
                            "has_client_certificate", "certificate_info"})
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
            "config": {"profile_prompt": None, "model_id": None, "mcp_servers": None, "context_window": None,
                       "structured_output": None, "next_steps": None}, "encrypted_payload": None}


def validate_next_steps(value):
    if value is not None and (not isinstance(value, dict) or set(value) != {"enabled", "every"}
            or type(value["enabled"]) is not bool or type(value["every"]) is not int or value["every"] < 1):
        raise CoreError("SETTINGS_INVALID")
    return copy.deepcopy(value)


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
        """Header-only version1 envelopes stay readable; new writes use the bound version2 envelope."""
        if row["encrypted_payload"] is None:
            return {}
        if self._cipher is None:
            raise CoreError("SETTINGS_CREDENTIAL_UNAVAILABLE")
        try:
            envelope = json.loads(self._cipher.decrypt(bytes(row["encrypted_payload"])))
            version, field = envelope["version"], ("headers" if envelope["version"] == 1 else "servers")
            if (version not in (1, 2) or set(envelope) != {"version", "tenant_id", "revision", field}
                    or envelope["tenant_id"] != row["tenant_id"] or envelope["revision"] != row["revision"]
                    or not isinstance(envelope[field], dict)):
                raise ValueError()
            entries = envelope[field]
            servers = {item["name"]: item for item in row["config"]["mcp_servers"] or ()}
            for name, value in entries.items():
                if (not isinstance(name, str) or not isinstance(value, dict) or not value
                        or name not in servers):
                    raise ValueError()
                if not set(value) <= ({"header_name", "header_value"} if version == 1 else {
                        "header_name", "header_value", "proxy_auth", "client_certificate"}):
                    raise ValueError()
                header = {key for key in ("header_name", "header_value") if key in value}
                if header and header != {"header_name", "header_value"}:
                    raise ValueError()
                if header:
                    if (not isinstance(value["header_name"], str)
                            or value["header_name"] != servers[name]["header_name"]):
                        raise ValueError()
                    _header_value(value["header_value"])
                if version == 1 and (set(value) != {"header_name", "header_value"}):
                    raise ValueError()
                if "proxy_auth" in value and not validate_stored_proxy_credentials(value["proxy_auth"]):
                    raise ValueError()
                if ("client_certificate" in value
                        and not validate_stored_client_certificate(value["client_certificate"])):
                    raise ValueError()
            return entries
        except (InvalidToken, ValueError, TypeError, KeyError, CoreError, UnicodeError):
            raise CoreError("SETTINGS_CREDENTIAL_UNAVAILABLE") from None

    def _identity(self, item, previous, previous_secrets, *, inherited):
        """Resolve the per-connection proxy address and write-only secret material.

        Every field is write-only in the same way the auth header already is: absent
        preserves, ``null`` clears, and a value replaces. Carrying proxy credentials or
        a client identity across a changed proxy or destination URL is refused, because
        that would transplant one connection's secret onto another endpoint.
        """
        invalid = CoreError("SETTINGS_INVALID")
        previous = previous or {}
        previous_proxy = previous_secrets.get("proxy_auth")
        previous_certificate = previous_secrets.get("client_certificate")
        proxy_url = previous.get("proxy_url")
        # An explicit null has to stay distinguishable from "never configured": on a link to a
        # deployment connection it is the only thing that may clear the trusted proxy address.
        proxy_declared = "proxy_url" in item or bool(previous.get("proxy_declared"))
        if "proxy_url" in item:
            proxy_url = validate_proxy_url(item["proxy_url"]) if item["proxy_url"] is not None else None
        elif proxy_url is None and not proxy_declared and inherited:
            original = next(server for server in self._deployment_mcp if server["name"] == item["name"])
            proxy_url = original["transport"].get("proxy_url")
        pair = {key for key in ("proxy_username", "proxy_password") if key in item}
        if pair and pair != {"proxy_username", "proxy_password"}:
            raise invalid
        proxy_preserved = not pair
        proxy_auth = previous_proxy
        if pair:
            username, password = item["proxy_username"], item["proxy_password"]
            if username is None and password is None:
                proxy_auth = None
            elif not isinstance(username, str) or not isinstance(password, str):
                raise invalid
            else:
                proxy_auth = validate_proxy_credentials(username, password)
        if "proxy_url" in item and item["proxy_url"] is None:
            if not proxy_preserved and proxy_auth is not None:
                raise invalid
            proxy_auth = None
        if proxy_auth is not None and proxy_url is None:
            raise invalid
        address_changed = "proxy_url" in item and previous.get("proxy_url") != proxy_url
        destination_changed = bool(previous) and previous.get("url") != item["url"]
        if proxy_preserved and proxy_auth is not None and (address_changed or destination_changed):
            raise invalid
        certificate_preserved = "client_certificate" not in item
        certificate = previous_certificate
        info = previous.get("certificate_info")
        if not certificate_preserved:
            supplied = item["client_certificate"]
            if supplied is None:
                certificate, info = None, None
            else:
                certificate = normalize_client_certificate(supplied)
                info = public_certificate_info(certificate["certificate_pem"])
        if certificate_preserved and certificate is not None and destination_changed:
            raise invalid
        return proxy_url, proxy_auth, certificate, info, proxy_declared

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
        if "context_window" in values:
            window = values["context_window"]
            if window is not None and (type(window) is not int or not 1024 <= window <= 100_000_000):
                raise CoreError("SETTINGS_INVALID")
            config["context_window"] = window
        if "structured_output" in values:
            policy = values["structured_output"]
            config["structured_output"] = validate_structured_output(policy) if policy is not None else None
        if "next_steps" in values:
            config["next_steps"] = validate_next_steps(values["next_steps"])
        if "guardrails_enabled" in values:
            if type(values["guardrails_enabled"]) is not bool:
                raise CoreError("SETTINGS_INVALID")
            config["guardrails_enabled"] = values["guardrails_enabled"]
        if "mcp_servers" in values:
            supplied = values["mcp_servers"]
            if supplied is not None and (not isinstance(supplied, list) or len(supplied) > 32):
                raise CoreError("SETTINGS_INVALID")
            old = {item["name"]: item for item in config["mcp_servers"] or ()}
            updated, new_secrets = [], {}
            for item in supplied or ():
                required = {"name", "url", "enabled", "header_name"}
                optional = required | {"header_value", "proxy_url", "proxy_username", "proxy_password",
                                       "client_certificate"}
                if not isinstance(item, dict) or not required <= item.keys() <= optional | DERIVED_FIELDS:
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
                prior, prior_secrets = old.get(name), secrets.get(name) or {}
                secret = item.get("header_value")
                if "header_value" not in item and "header_value" in prior_secrets:
                    if prior["url"] != url or prior["header_name"] != header:
                        raise CoreError("SETTINGS_INVALID")
                    secret = prior_secrets["header_value"]
                entry = {}
                if secret is not None:
                    if not header:
                        raise CoreError("SETTINGS_INVALID")
                    try:
                        _header_value(secret)
                    except CoreError:
                        raise CoreError("SETTINGS_INVALID") from None
                    entry = {"header_name": header, "header_value": secret}
                proxy_url, proxy_auth, certificate, certificate_info, proxy_declared = self._identity(
                    item, prior, prior_secrets, inherited=inherited)
                if proxy_auth is not None:
                    entry["proxy_auth"] = proxy_auth
                if certificate is not None:
                    entry["client_certificate"] = certificate
                if entry:
                    new_secrets[name] = entry
                updated.append({**{key: item[key] for key in required}, "has_header_value": secret is not None,
                                "proxy_url": proxy_url, "proxy_declared": proxy_declared,
                                "has_proxy_credentials": proxy_auth is not None,
                                "has_client_certificate": certificate is not None,
                                "certificate_info": certificate_info})
            config["mcp_servers"] = updated if supplied is not None else None
            secrets = new_secrets
        row = {"tenant_id": previous["tenant_id"], "revision": previous["revision"] + 1,
               "storage_version": 1, "config": config, "encrypted_payload": None, "actor_id": actor_id}
        if secrets:
            if self._cipher is None:
                raise CoreError("SETTINGS_CREDENTIAL_UNAVAILABLE")
            row["encrypted_payload"] = self._cipher.encrypt(json.dumps({"version": 2,
                "tenant_id": row["tenant_id"], "revision": row["revision"], "servers": secrets},
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
        next_steps = copy.deepcopy(config.get("next_steps") or {
            "enabled": agent.next_steps_every is not None,
            "every": agent.next_steps_every or agent.next_steps_default_every,
        })
        next_steps["enabled"] = next_steps["enabled"] and agent.next_steps_available
        servers = config["mcp_servers"]
        if servers is None:
            servers = [{"name": item["name"], "url": safe_url(item["transport"]["url"]), "enabled": True,
                        "header_name": "", "has_header_value": False,
                        "proxy_url": (safe_url(item["transport"]["proxy_url"])
                                      if item["transport"].get("proxy_url") else None),
                        "has_proxy_credentials": False,
                        "has_client_certificate": False, "certificate_info": None}
                       for item in agent.platform_mcp]
        else:
            servers = [{**{key: value for key, value in item.items() if key != "proxy_declared"},
                        "has_header_value": item.get("has_header_value", False),
                        "proxy_url": item.get("proxy_url"),
                        "has_proxy_credentials": item.get("has_proxy_credentials", False),
                        "has_client_certificate": item.get("has_client_certificate", False),
                        "certificate_info": copy.deepcopy(item.get("certificate_info"))}
                       for item in servers]
        return {"revision": row["revision"], "profile_prompt": config["profile_prompt"] if config["profile_prompt"] is not None
                else agent.agent_config.agent.get("profile_prompt", ""), "model_id": config["model_id"] if config["model_id"] is not None
                else agent.agent_config.model["route"], "mcp_servers": servers,
                "context_window": config.get("context_window") or agent.context_window,
                "structured_output": copy.deepcopy(config.get("structured_output") or STRUCTURED_OUTPUT_DEFAULT),
                "guardrails_enabled": config.get("guardrails_enabled", True),
                "next_steps": next_steps, "next_steps_available": agent.next_steps_available,
                "inherits": {key: config.get(key) is None for key in CONFIG_FIELDS}}

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
            transport = declaration.setdefault("transport", {})
            if server.get("proxy_url"):
                transport["proxy_url"] = server["proxy_url"]
            elif server.get("proxy_declared"):
                transport.pop("proxy_url", None)
            if server.get("has_proxy_credentials") or server.get("has_client_certificate"):
                # A secret-free marker: an inherited deployment connection keeps its own headers and
                # tool semantics, yet still has to be rehydrated from the pinned revision. Only real
                # secret material needs that, so a plain deployment link stays byte-identical.
                declaration["owner_transport_configured"] = True
            if credentials:
                secret = secrets.get(server["name"]) or {}
                if not inherited:
                    declaration["headers"] = ({secret["header_name"]: secret["header_value"]}
                                              if "header_value" in secret else {})
                if secret.get("proxy_auth"):
                    declaration["proxy_auth"] = copy.deepcopy(secret["proxy_auth"])
                if secret.get("client_certificate"):
                    declaration["client_certificate"] = copy.deepcopy(secret["client_certificate"])
            selected.append(declaration)
        platform = replace(platform, allowed_mcp_servers=platform.allowed_mcp_servers | {item["name"] for item in selected})
        if profile:
            raw["tools"]["mcp"]["allow_servers"] = [item["name"] for item in selected]
            raw["tools"]["mcp"]["owner_servers"] = [item["name"] for item in selected if item.get("owner_configured")]
        return raw, platform, tuple(selected)
