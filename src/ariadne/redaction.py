"""Shared redaction for model-facing and operator-facing errors."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from urllib.parse import quote

_SENSITIVE_ENVIRONMENT_NAME = re.compile(
    r"(?:^|_)(?:PASSWORD|PASS|TOKEN|SECRET|API_KEY|PRIVATE_KEY|AUTHORIZATION|"
    r"CREDENTIALS?|COOKIE|USERNAME)(?:_|$)",
    re.IGNORECASE,
)
_SENSITIVE_FIELD = (
    r"(?:password|passphrase|app[_-]?password|token|access[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|session[_-]?token|secret|client[_-]?secret|"
    r"api[_-]?key|x[_-]?api[_-]?key|private[_-]?key|auth|authorization|"
    r"proxy[_-]?authorization|credential|cookie|set[_-]?cookie|username)"
)
_SECRET_HEADER = re.compile(rf"(?im)^(?P<prefix>\s*{_SENSITIVE_FIELD}\s*:\s*)[^\r\n]*")
_QUOTED_SECRET = re.compile(
    rf"(?is)(?P<prefix>[\"']?{_SENSITIVE_FIELD}[\"']?\s*[:=]\s*)"
    rf"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)
_UNQUOTED_SECRET = re.compile(
    rf"(?i)(?P<prefix>\b{_SENSITIVE_FIELD}\b\s*[:=]\s*)"
    r"(?P<value>[^\s,;&}<]+)"
)
_XML_SECRET = re.compile(
    rf"(?is)<(?P<open>(?:[A-Za-z_][\w.-]*:)?(?P<tag>{_SENSITIVE_FIELD}))"
    rf"(?P<attrs>\s[^>]*)?>.*?</(?P=open)>"
)
_AUTH_SCHEME = re.compile(r"(?i)\b(?:basic|bearer)\s+[a-z0-9+/=_.:-]+")
_URL_USERINFO = re.compile(r"(?i)(?P<scheme>https?://)[^/@\s]+@")
_SECRET_QUERY = re.compile(rf"(?i)(?P<prefix>[?&]{_SENSITIVE_FIELD}=)[^&#\s]*")


def _sensitive_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    values: set[str] = set()
    for name, value in environment.items():
        if not value or _SENSITIVE_ENVIRONMENT_NAME.search(name) is None:
            continue
        values.add(value)
        encoded = quote(value, safe="")
        if encoded != value:
            values.add(encoded)
    return tuple(sorted(values, key=len, reverse=True))


def redact_sensitive_text(
    text: str, environment: Mapping[str, str] | None = None
) -> str:
    """Remove configured credentials and common secret-bearing syntax."""
    redacted = _AUTH_SCHEME.sub("[REDACTED]", text)
    for value in _sensitive_values(
        environment if environment is not None else os.environ
    ):
        if len(value) >= 4:
            redacted = redacted.replace(value, "[REDACTED]")
        else:
            redacted = re.sub(
                rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])",
                "[REDACTED]",
                redacted,
            )
    redacted = _URL_USERINFO.sub(r"\g<scheme>[REDACTED]@", redacted)
    redacted = _SECRET_QUERY.sub(r"\g<prefix>[REDACTED]", redacted)
    redacted = _SECRET_HEADER.sub(r"\g<prefix>[REDACTED]", redacted)
    redacted = _QUOTED_SECRET.sub(r"\g<prefix>\g<quote>[REDACTED]\g<quote>", redacted)
    redacted = _UNQUOTED_SECRET.sub(r"\g<prefix>[REDACTED]", redacted)
    redacted = _XML_SECRET.sub(
        lambda match: (
            f"<{match.group('open')}{match.group('attrs') or ''}>"
            f"[REDACTED]</{match.group('open')}>"
        ),
        redacted,
    )
    return redacted


__all__ = ["redact_sensitive_text"]
