"""Initialize the optional local Keycloak using deployment environment only."""
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import psycopg


class LocalConfigError(ValueError):
    """Configuration diagnostic containing names only."""


def main():
    required = (
        "DATABASE_URL", "KEYCLOAK_ISSUER_URL", "KEYCLOAK_CLIENT_ID",
        "KEYCLOAK_CLIENT_SECRET", "KEYCLOAK_UI_CLIENT_ID", "KEYCLOAK_AUDIENCE",
        "LOCAL_OWNER_PASSWORD", "LOCAL_KEYCLOAK_ADMIN_PASSWORD", "AGENT_URL",
    )
    for name in required:
        if not os.environ.get(name, "").strip():
            raise LocalConfigError(f"Missing local setting: {name}")
    issuer = urlsplit(os.environ["KEYCLOAK_ISSUER_URL"])
    if (issuer.scheme != "http" or issuer.hostname != "localhost"
            or issuer.port != 8085 or issuer.username or issuer.password
            or issuer.query or issuer.fragment):
        raise LocalConfigError("Invalid local setting: KEYCLOAK_ISSUER_URL")
    prefix = "/realms/"
    realm = issuer.path.removeprefix(prefix)
    if not issuer.path.startswith(prefix) or not realm or not all(c.isalnum() or c in "-_" for c in realm):
        raise LocalConfigError("Invalid local setting: KEYCLOAK_ISSUER_URL")
    origin = urlsplit(os.environ["AGENT_URL"])
    # AGENT_URL may be loopback or a local peer alias (agent-a / agent-b).
    # Keycloak web origins below use only its port, so the UI stays on localhost.
    if (origin.scheme != "http"
            or (origin.hostname not in ("localhost", "127.0.0.1") and origin.hostname not in ("agent-a", "agent-b"))
            or origin.username or origin.password or origin.query or origin.fragment
            or origin.path not in ("", "/")):
        raise LocalConfigError("Invalid local setting: AGENT_URL")
    api_id, ui_id = (os.environ[name] for name in ("KEYCLOAK_CLIENT_ID", "KEYCLOAK_UI_CLIENT_ID"))
    if api_id == ui_id:
        raise LocalConfigError("Local Keycloak requires separate API and UI clients")
    port = origin.port or 80
    origins = [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]
    owner_role = os.environ.get("KEYCLOAK_OWNER_ROLE", "agent-owner")
    external_role = os.environ.get("KEYCLOAK_EXTERNAL_ROLE", "agent-external")
    if not owner_role or not external_role or owner_role == external_role:
        raise LocalConfigError("Local Keycloak requires separate owner and external roles")
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        connection.execute("CREATE SCHEMA IF NOT EXISTS keycloak")
    directory = Path("/realm/import")
    directory.mkdir(parents=True, exist_ok=True)
    for parent in (directory.parent, directory):
        os.chown(parent, 1000, 0)
    os.chmod(directory, 0o700)
    destination = directory / f"{realm}-realm.json"
    if destination.exists():
        print("Local Keycloak import already exists; existing credentials retained")
        return
    audience = {"name": "api-audience", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
                "config": {"included.client.audience": os.environ["KEYCLOAK_AUDIENCE"],
                           "access.token.claim": "true", "id.token.claim": "false", "introspection.token.claim": "true"}}
    roles = {"name": "realm-roles", "protocol": "openid-connect", "protocolMapper": "oidc-usermodel-realm-role-mapper",
             "config": {"multivalued": "true", "claim.name": "realm_access.roles", "jsonType.label": "String",
                        "access.token.claim": "true", "introspection.token.claim": "true"}}
    validators = {
        "username": {"length": {"min": 3, "max": 255}, "username-prohibited-characters": {},
                     "up-username-not-idn-homograph": {}},
        "email": {"email": {}, "length": {"max": 255}},
        "firstName": {"length": {"max": 255}, "person-name-prohibited-characters": {}},
        "lastName": {"length": {"max": 255}, "person-name-prohibited-characters": {}},
    }
    profile = {"attributes": [
        {"name": name, "validations": validation, "multivalued": False,
         "permissions": {"view": ["admin", "user"], "edit": ["admin", "user"]}}
        for name, validation in validators.items()
    ], "groups": []}
    document = {
        "realm": realm, "enabled": True, "sslRequired": "none", "registrationAllowed": False,
        "registrationEmailAsUsername": False, "verifyEmail": False,
        "attributes": {"CreateDefaultClientScopes": "true"},
        "components": {"org.keycloak.userprofile.UserProfileProvider": [
            {"providerId": "declarative-user-profile", "config": {"kc.user.profile.config": [json.dumps(profile)]}}
        ]},
        "roles": {"realm": [{"name": owner_role}, {"name": external_role}]},
        "clientScopes": [{"name": "core-agent-audience", "protocol": "openid-connect",
                          "attributes": {"include.in.token.scope": "true"}, "protocolMappers": [audience, roles]}],
        "clients": [
            {"clientId": api_id, "enabled": True, "protocol": "openid-connect", "publicClient": False,
             "secret": os.environ["KEYCLOAK_CLIENT_SECRET"], "standardFlowEnabled": False,
             "directAccessGrantsEnabled": False, "serviceAccountsEnabled": False},
            {"clientId": ui_id, "enabled": True, "protocol": "openid-connect", "publicClient": True,
             "standardFlowEnabled": True, "directAccessGrantsEnabled": False, "fullScopeAllowed": True,
             "redirectUris": [origin + "/ui/*" for origin in origins], "webOrigins": origins,
             "attributes": {"pkce.code.challenge.method": "S256", "post.logout.redirect.uris": "+"},
             "defaultClientScopes": ["basic", "roles", "profile", "email", "web-origins", "core-agent-audience"]},
        ],
        "users": [{"username": os.environ.get("LOCAL_OWNER_USERNAME", "owner"), "enabled": True,
                   "requiredActions": [], "realmRoles": [owner_role],
                   "clientRoles": {"realm-management": ["manage-clients", "manage-users", "manage-realm",
                                                       "view-clients", "view-users", "view-realm", "create-client"]},
                   "credentials": [{"type": "password", "value": os.environ["LOCAL_OWNER_PASSWORD"], "temporary": False}]}],
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), 1000, 0)
            os.fchmod(stream.fileno(), 0o600)
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    print("Local Keycloak realm import initialized")


if __name__ == "__main__":
    try:
        main()
    except LocalConfigError as error:
        # Validation messages contain setting names, never their values.
        print(str(error))
        raise SystemExit(1)
    except Exception:
        print("Local Keycloak initialization failed")
        raise SystemExit(1)
