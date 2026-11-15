from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password)"
        r"\s*[:=]\s*[\"']?)[^\s\"',;}]{4,}"
    ),
    re.compile(r"(?i)\b(?:sk|ghp|github_pat|gho|xox[baprs])-[A-Za-z0-9_-]{12,}\b"),
]
_SENSITIVE_KEYS = re.compile(r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|credential)")


def _replacement(match: re.Match[str]) -> str:
    prefix = match.group(1) if match.re.groups else ""
    return prefix + "[REDACTED]"


def redact_text(value: str, secrets: Iterable[str] = ()) -> str:
    result = value
    for secret in sorted({item for item in secrets if len(item) >= 4}, key=len, reverse=True):
        result = result.replace(secret, "[REDACTED]")
    for pattern in _PATTERNS:
        result = pattern.sub(_replacement, result)
    return result


def redact_value(value: Any) -> Any:
    """Redact secret-shaped mapping fields before JSON or SQLite serialization."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            result[key_text] = "[REDACTED]" if _SENSITIVE_KEYS.search(key_text) else redact_value(item)
        return result
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


class StreamingRedactor:
    """Mask known secrets with bounded memory and fail closed on enormous tokens."""

    def __init__(self, secrets: Iterable[str] = (), overlap: int = 8192):
        self.secrets = tuple(sorted({item for item in secrets if len(item) >= 4}, key=len, reverse=True))
        self.overlap = max(overlap, max((len(item) for item in self.secrets), default=0) + 16)
        self.buffer = ""
        self._discarding_token = False
        self.truncated = False

    def feed(self, data: bytes, *, final: bool = False) -> bytes:
        incoming = data.decode("utf-8", errors="replace")
        if self._discarding_token:
            delimiter = next((i for i, char in enumerate(incoming) if char.isspace()), None)
            if delimiter is None:
                keep = max(self.overlap, max((len(item) for item in self.secrets), default=0) + 16)
                self.buffer = incoming[-keep:]
                if final:
                    self.buffer = ""
                    self._discarding_token = False
                return b""
            incoming = incoming[delimiter:]
            self.buffer = ""
            self._discarding_token = False

        self.buffer += incoming
        if final:
            safe, self.buffer = self.buffer, ""
            return redact_text(safe, self.secrets).encode("utf-8")
        if len(self.buffer) <= self.overlap:
            return b""
        boundary = len(self.buffer) - self.overlap
        delimiter = max(
            self.buffer.rfind(" ", 0, boundary),
            self.buffer.rfind("\n", 0, boundary),
            self.buffer.rfind("\t", 0, boundary),
            self.buffer.rfind("\r", 0, boundary),
        )
        if delimiter >= 0:
            safe = self.buffer[: delimiter + 1]
            self.buffer = self.buffer[delimiter + 1 :]
            return redact_text(safe, self.secrets).encode("utf-8")

        # One delimiter-free token exceeded the overlap; drop it rather than expose a possible secret.
        self.buffer = ""
        self._discarding_token = True
        self.truncated = True
        return b"[REDACTED]"
