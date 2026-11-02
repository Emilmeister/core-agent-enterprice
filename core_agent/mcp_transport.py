"""Per-MCP proxy and mTLS with shared packaged trust, independent of process ENV.

Proxy and destination have separate SSL contexts; only the destination loads
client identity. Private temporary key files are removed immediately after load.
"""

from __future__ import annotations

import base64
import os
import shutil
import ssl
import tempfile
from urllib.parse import urlparse

import certifi
import httpx

from .errors import CoreError
from .mcp_credentials import validate_proxy_url, validate_proxy_credentials

MAX_IDENTITY_BYTES = 128 * 1024
_PACKAGED_TRUSTED_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "certs", "russian-trusted-root-ca.pem"
)


def trust_bundle_paths():
    """The CA files every MCP peer — proxy and destination — is verified against.

    Tests patch this single factory to point at a generated authority; no
    other trust decision is reachable from the declaration.
    """
    return (certifi.where(), _PACKAGED_TRUSTED_ROOT)


def _trusted_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    try:
        for path in trust_bundle_paths():
            context.load_verify_locations(cafile=path)
    except (OSError, ssl.SSLError):
        raise CoreError("CONFIG_INVALID", "MCP TLS trust store is unavailable") from None
    return context


def proxy_ssl_context():
    """A context that verifies the proxy but can never present client identity."""
    return _trusted_context()


def destination_ssl_context(client_certificate=None):
    """Verify the MCP endpoint, optionally presenting the configured identity."""
    context = _trusted_context()
    if client_certificate is None:
        return context
    if not isinstance(client_certificate, dict):
        raise CoreError("CONFIG_INVALID", "MCP client identity must be an object")
    certificate = client_certificate.get("certificate_pem")
    private_key = client_certificate.get("private_key_pem")
    if (
        not isinstance(certificate, str)
        or not certificate
        or not isinstance(private_key, str)
        or not private_key
    ):
        raise CoreError(
            "CONFIG_INVALID",
            "MCP client identity requires certificate_pem and private_key_pem",
        )
    if (
        len(certificate.encode("utf-8")) + len(private_key.encode("utf-8"))
        > MAX_IDENTITY_BYTES
    ):
        raise CoreError("CONFIG_INVALID", "MCP client identity is too large")
    directory = tempfile.mkdtemp(prefix="mcp-identity-")  # mode 0700
    try:
        paths = {}
        for kind, text in (("certificate", certificate), ("key", private_key)):
            path = os.path.join(directory, f"identity-{kind}.pem")
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            paths[kind] = path
        try:
            context.load_cert_chain(
                certfile=paths["certificate"], keyfile=paths["key"]
            )
        except (ssl.SSLError, OSError):
            # Never echo PEM material or parser details into the error.
            raise CoreError(
                "MCP_CONNECTION_FAILED", "MCP client identity could not be loaded"
            ) from None
    except (OSError, UnicodeError):
        raise CoreError("MCP_CONNECTION_FAILED", "MCP client identity could not be loaded") from None
    finally:
        try:
            shutil.rmtree(directory)
        except OSError:
            raise CoreError("MCP_CONNECTION_FAILED", "MCP client identity cleanup failed") from None
    return context


def _proxy_credentials(declaration):
    auth = declaration.get("proxy_auth")
    if auth is None:
        return None
    if not isinstance(auth, dict) or set(auth) != {"username", "password"}:
        raise CoreError("CONFIG_INVALID", "MCP proxy_auth must be an object")
    username = auth.get("username")
    password = auth.get("password")
    try:
        validate_proxy_credentials(username, password)
    except CoreError:
        raise CoreError("CONFIG_INVALID", "MCP proxy_auth is invalid") from None
    return (username, password)


def http_client_options(declaration):
    """httpx client kwargs for one MCP connection, plus its secret values.

    The second element lists credential values that must be redacted from any
    error, result or telemetry payload leaving the connector.
    """
    transport = declaration.get("transport") or {}
    proxy_url = transport.get("proxy_url")
    credentials = _proxy_credentials(declaration)
    if proxy_url is None and credentials is not None:
        # Credentials without a proxy would be dead weight or a mistake;
        # never let them travel anywhere by accident.
        raise CoreError("CONFIG_INVALID", "MCP proxy credentials require a proxy_url")
    options = {
        "trust_env": False,
        "verify": destination_ssl_context(declaration.get("client_certificate")),
    }
    if proxy_url is not None:
        try:
            proxy_url = validate_proxy_url(proxy_url)
        except CoreError:
            raise CoreError("CONFIG_INVALID", "MCP proxy_url is invalid") from None
        proxy_kwargs = {"auth": credentials}
        if urlparse(proxy_url).scheme == "https":
            # Explicit shared trust, with no destination client identity.
            proxy_kwargs["ssl_context"] = proxy_ssl_context()
        options["proxy"] = httpx.Proxy(url=proxy_url, **proxy_kwargs)
    secrets = ()
    if credentials:
        encoded = base64.b64encode(":".join(credentials).encode("utf-8")).decode("ascii")
        secrets = (credentials[1], encoded)
    return options, secrets
