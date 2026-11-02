"""Bounded validation of per-connection proxy and mTLS material for owner MCP settings.

Only this module parses owner-supplied proxy addresses and client identity. The
normalized result holds the transport-facing proxy URL plus the secret half
(proxy credentials and unencrypted PEM chain/key) which the settings store
encrypts into the immutable revision row. Passwords used for the upload never
survive normalization.
"""

import base64
import binascii
import datetime
import re
from urllib.parse import urlparse

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)
from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates

from .errors import CoreError

MAX_PROXY_URL_BYTES = 4096
MAX_PROXY_CREDENTIAL_BYTES = 4096
MAX_CREDENTIAL_BYTES = 128 * 1024
MAX_NAME_CHARS = 512
PROXY_SCHEMES = frozenset({"http", "https"})
CLIENT_CERTIFICATE_FIELDS = frozenset({"certificate_pem", "private_key_pem"})
PROXY_CREDENTIAL_FIELDS = frozenset({"username", "password"})
BASIC_AUTH_SEPARATOR = ":"


def _invalid():
    return CoreError("SETTINGS_INVALID")


def utf8_bytes(value):
    """UTF-8 bytes of owner text, or a safe settings error for unencodable input."""
    try:
        return value.encode("utf-8")
    except (AttributeError, UnicodeError):
        raise _invalid() from None


def _plain_text(value, maximum, *, empty=False):
    """Bounded text without control characters; the caller decides about emptiness."""
    if not isinstance(value, str) or (not empty and not value):
        return False
    return (len(utf8_bytes(value)) <= maximum
            and not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value))


def validate_proxy_url(value):
    """Return the accepted proxy address, or reject anything the transport must not dial."""
    if not _plain_text(value, MAX_PROXY_URL_BYTES) or any(char.isspace() for char in value):
        raise _invalid()
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        raise _invalid() from None
    if (
        parsed.scheme not in PROXY_SCHEMES
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        # A bare "?" or "#" is still a query/fragment separator, so the raw text is checked
        # before the parser can silently drop an empty component.
        or "?" in value
        or "#" in value
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or "\\" in parsed.netloc
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise _invalid()
    return value


def validate_proxy_credentials(username, password):
    """Validate the write-only proxy pair as one unit.

    HTTP Basic joins the two with ``:``, so the separator cannot appear in a username; an empty
    password is legitimate and stays allowed.
    """
    if not _plain_text(username, MAX_PROXY_CREDENTIAL_BYTES) or BASIC_AUTH_SEPARATOR in username:
        raise _invalid()
    if not _plain_text(password, MAX_PROXY_CREDENTIAL_BYTES, empty=True):
        raise _invalid()
    return {"username": username, "password": password}


def _upload_password(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(utf8_bytes(value)) > MAX_PROXY_CREDENTIAL_BYTES:
        raise _invalid()
    return value


def _public_key_bytes(key):
    return key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)


def _dump_certificate(certificate):
    return certificate.public_bytes(Encoding.PEM).decode("ascii")


def _dump_private_key(private_key):
    return private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode("ascii")


def _check_leaf(leaf, private_key):
    """The leaf must be valid right now and belong to the supplied private key."""
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        if leaf.not_valid_before_utc > now or leaf.not_valid_after_utc <= now:
            raise _invalid()
        if _public_key_bytes(leaf.public_key()) != _public_key_bytes(private_key.public_key()):
            raise _invalid()
    except CoreError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise _invalid() from None


def _finish(certificate_pem, private_key_pem):
    """The normalized identity is bounded before it can be stored."""
    chain, key = utf8_bytes(certificate_pem), utf8_bytes(private_key_pem)
    if len(chain) + len(key) > MAX_CREDENTIAL_BYTES:
        raise _invalid()
    return {"certificate_pem": certificate_pem, "private_key_pem": private_key_pem}


def _from_pkcs12(value):
    data = value.get("data")
    password = _upload_password(value.get("password"))
    if not isinstance(data, str) or len(data) > MAX_CREDENTIAL_BYTES * 2:
        raise _invalid()
    try:
        decoded = base64.b64decode(utf8_bytes(data), validate=True)
    except (binascii.Error, UnicodeError, ValueError):
        raise _invalid() from None
    if not decoded or len(decoded) > MAX_CREDENTIAL_BYTES:
        raise _invalid()
    try:
        private_key, leaf, others = load_key_and_certificates(
            decoded, utf8_bytes(password) if password is not None else None
        )
    except (ValueError, TypeError, UnsupportedAlgorithm, UnicodeError):
        raise _invalid() from None
    if private_key is None or not isinstance(leaf, x509.Certificate):
        raise _invalid()
    chain = [leaf] + [item for item in others or () if isinstance(item, x509.Certificate)]
    _check_leaf(leaf, private_key)
    return _finish("".join(_dump_certificate(item) for item in chain), _dump_private_key(private_key))


def _load_private_key(key_bytes, password):
    """Load an owner key, keeping an encrypted key's real password failures visible."""
    try:
        return load_pem_private_key(key_bytes, password=None)
    except TypeError:
        pass
    except (ValueError, UnsupportedAlgorithm):
        raise _invalid() from None
    if password is None:
        raise _invalid()
    try:
        return load_pem_private_key(key_bytes, password=utf8_bytes(password))
    except (TypeError, ValueError, UnsupportedAlgorithm, UnicodeError):
        raise _invalid() from None


def _from_pem(value):
    certificate, key_pem = value.get("certificate"), value.get("private_key")
    password = _upload_password(value.get("password"))
    if not isinstance(certificate, str) or not isinstance(key_pem, str):
        raise _invalid()
    certificate_bytes, key_bytes = utf8_bytes(certificate), utf8_bytes(key_pem)
    if len(certificate_bytes) + len(key_bytes) > MAX_CREDENTIAL_BYTES:
        raise _invalid()
    try:
        chain = list(x509.load_pem_x509_certificates(certificate_bytes))
    except (ValueError, TypeError):
        raise _invalid() from None
    if not chain:
        raise _invalid()
    private_key = _load_private_key(key_bytes, password)
    _check_leaf(chain[0], private_key)
    return _finish("".join(_dump_certificate(item) for item in chain), _dump_private_key(private_key))


def normalize_client_certificate(value):
    """Return the storable normalized identity, or raise a safe settings error."""
    if not isinstance(value, dict):
        raise _invalid()
    upload_format = value.get("format")
    if upload_format == "pkcs12":
        required, optional = {"format", "data"}, {"password"}
    elif upload_format == "pem":
        required, optional = {"format", "certificate", "private_key"}, {"password"}
    else:
        raise _invalid()
    if not required <= value.keys() <= required | optional:
        raise _invalid()
    return _from_pkcs12(value) if upload_format == "pkcs12" else _from_pem(value)


def validate_stored_proxy_credentials(value):
    """Re-check proxy material read back from an encrypted revision envelope."""
    if not isinstance(value, dict) or set(value) != PROXY_CREDENTIAL_FIELDS:
        return False
    try:
        validate_proxy_credentials(value["username"], value["password"])
    except (CoreError, TypeError):
        return False
    return True


def validate_stored_client_certificate(value):
    """Stored identity is exactly the normalized PEM pair, still inside the size bound."""
    if not isinstance(value, dict) or set(value) != CLIENT_CERTIFICATE_FIELDS:
        return False
    total = 0
    try:
        for item in value.values():
            if not isinstance(item, str) or not item:
                return False
            total += len(item.encode("utf-8"))
    except UnicodeError:
        return False
    return total <= MAX_CREDENTIAL_BYTES


def _safe_name(value):
    text = "".join(char if 32 <= ord(char) < 127 else "?" for char in value)[:MAX_NAME_CHARS]
    return re.sub(r"\s+", " ", text).strip()


def certificate_info(certificate_pem):
    """Public display fields for a stored chain: never the key, password or PEM itself."""
    try:
        leaf = x509.load_pem_x509_certificate(certificate_pem.encode("utf-8"))
        expiry = leaf.not_valid_after_utc
    except (AttributeError, TypeError, ValueError):
        raise _invalid() from None
    return {
        "subject": _safe_name(leaf.subject.rfc4514_string()),
        "issuer": _safe_name(leaf.issuer.rfc4514_string()),
        "expiry": expiry.isoformat(),
        "sha256": leaf.fingerprint(SHA256()).hex(),
    }
