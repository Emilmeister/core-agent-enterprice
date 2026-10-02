from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from .errors import CoreError


def validate_http_url(value, setting):
    """Validate configured transport URLs without exposing parser diagnostics."""
    try:
        if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError
        parsed = urlparse(value)
        port = parsed.port
        authority = parsed.netloc.rsplit("@", 1)[-1]
        bracket_suffix = authority.split("]", 1)[1] if authority.startswith("[") else ""
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or "\\" in parsed.netloc or parsed.netloc.endswith(":")
                or (bracket_suffix and not bracket_suffix.startswith(":"))
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError
    except ValueError:
        raise CoreError("CONFIG_INVALID", f"{setting} must be a valid HTTP or HTTPS URL") from None
    return parsed


def safe_url(value):
    """Log-only URL projection; retain the original destination for transport."""
    try:
        parsed = validate_http_url(value, "URL")
        host = parsed.hostname
        if not host:
            return "[invalid URL]"
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host += f":{parsed.port}"
        return f"{parsed.scheme}://{host}{parsed.path}" if parsed.scheme else host
    except (CoreError, ValueError, TypeError):
        return "[invalid URL]"


def redact(value, known_secrets=()):
    secrets = {secret for secret in known_secrets if secret}

    def clean(item):
        if isinstance(item, dict):
            return {
                key: (
                    "[REDACTED]"
                    if isinstance(key, str)
                    and any(
                        marker in key.lower()
                        for marker in (
                            "authorization",
                            "api_key",
                            "password",
                            "secret",
                            "token",
                        )
                    )
                    else clean(val)
                )
                for key, val in item.items()
            }
        if isinstance(item, list):
            return [clean(val) for val in item]
        if isinstance(item, tuple):
            return tuple(clean(val) for val in item)
        if not isinstance(item, str):
            return item
        result = item
        result = re.sub(r"gh[pousr]_[A-Za-z0-9_]{20,}", "[REDACTED]", result)
        result = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED]", result)
        for secret in secrets:
            result = result.replace(secret, "[REDACTED]")
        result = re.sub(
            r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+", r"\1[REDACTED]", result
        )
        result = re.sub(r"(?i)(token\s*=\s*)[^\s]+", r"\1[REDACTED]", result)
        return result

    return clean(value)


def normalize_workspace_path(root, requested):
    lexical_root = Path(root)
    root_path = lexical_root.resolve()
    lexical_candidate = lexical_root / requested
    candidate = lexical_candidate.resolve(strict=False)
    try:
        candidate.relative_to(root_path)
    except ValueError:
        raise CoreError("POLICY_DENIED") from None
    return lexical_candidate


class RetryPolicy:
    def __init__(self, max_attempts=3):
        self.max_attempts = max_attempts

    def should_retry(self, *, read_only, attempt, outcome_known, idempotency_key):
        if attempt >= self.max_attempts:
            return False
        return read_only or (outcome_known and bool(idempotency_key))


class TenantStore:
    def __init__(self):
        self._items = {}

    def put(self, tenant, key, value):
        self._items[(tenant, key)] = value

    def get(self, tenant, key):
        try:
            return self._items[(tenant, key)]
        except KeyError:
            raise CoreError("NOT_FOUND") from None
