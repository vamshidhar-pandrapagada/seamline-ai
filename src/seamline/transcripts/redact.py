"""Strip secrets from transcript text before anything else (LLM, ledger, screen) sees it.

Patterns favor recall over precision: a redacted non-secret costs little, a leaked key a lot.
"""

from __future__ import annotations

import re
from pathlib import PurePath
from typing import Any

MASK = "[REDACTED]"

# Values that are secrets on their own, wherever they appear.
_TOKEN_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),  # Anthropic
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}"),  # OpenAI
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),  # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
]
# Credentials inside URLs: scheme://user:password@host
_URL_CREDS = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s@/]+)(@)", re.I)
# Bearer / Basic auth headers
_AUTH_HEADER = re.compile(r"(\b(?:Bearer|Basic)\s+)([A-Za-z0-9._~+/=-]{16,})", re.I)
# NAME=value or "name": "value" where the name sounds secret
_SECRET_WORDS = r"SECRET|TOKEN(?!S|IZ)|PASSWORD|PASSWD|PWD|_KEY|APIKEY|CREDENTIAL"
_SECRET_NAME = rf"[A-Za-z0-9_.-]*(?:{_SECRET_WORDS})[A-Za-z0-9_.-]*"
_ASSIGNMENT = re.compile(
    rf"""(?P<name>\b{_SECRET_NAME})(?P<sep>["']?\s*[:=]\s*["']?)(?P<value>[^\s"',;]{{4,}})""",
    re.I,
)


def redact(text: str) -> str:
    if not text:
        return text
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(MASK, text)
    text = _URL_CREDS.sub(rf"\1{MASK}\3", text)
    text = _AUTH_HEADER.sub(rf"\1{MASK}", text)
    text = _ASSIGNMENT.sub(_mask_assignment, text)
    return text


def redact_value(value: Any) -> Any:
    """Redact every string inside nested dicts and lists (tool inputs)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


def is_env_file(path: str | None) -> bool:
    """.env, .env.local, prod.env, … : files whose whole content should never be kept."""
    if not path:
        return False
    name = PurePath(path).name
    return name == ".env" or name.startswith(".env.") or name.endswith(".env")


def _mask_assignment(m: re.Match) -> str:
    if m.group("value") == MASK:
        return m.group(0)
    return f"{m.group('name')}{m.group('sep')}{MASK}"
