"""Keep operational text and money; remove secrets, bank identifiers and media.

Free text cannot be perfectly classified. Known credentials are removed literally as well
as by syntax. Numeric amounts/rates are not treated as generic phone/account numbers.
"""
from __future__ import annotations

import re
from typing import Any

SENSITIVE = re.compile(r"password|secret|token|authorization|cookie|api.?key|dsn|phone|bsuid|account.?number|bank.?account|iban|media|attachment|image|audio|base64|receipt.?extraction", re.I)
PATTERNS = [
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+)\b"),
    re.compile(r"(?i)\bBearer\s+[^\s\"']+"),
    re.compile(r"(?i)(?:password|secret|token|api[_-]?key)\s*[=:]\s*[^\s,;\"']+"),
    re.compile(r"\b\d{20}\b"),  # Venezuelan bank account
    re.compile(r"(?<!\d)(?:\+?58|0)(?:4(?:12|14|16|24|26))[- .]?\d{3}[- .]?\d{4}(?!\d)"),
    re.compile(r"(?i)https?://[^\s<>\"']+"),  # signed URLs, Telegram token URLs, credentials in URLs
    re.compile(r"(?i)\b(?:tel[eé]fono|phone|cuenta|iban)\s*[:=]\s*[+\d][\d .-]{6,}"),
]


class Scrubber:
    def __init__(self, secrets: list[str] | None = None):
        self.secrets = sorted({s for s in (secrets or []) if len(s) >= 8}, key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[SECRET]")
        for pattern in PATTERNS:
            value = pattern.sub("[REDACTED]", value)
        return value

    def clean(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): self.clean(v) for k, v in value.items() if not SENSITIVE.search(str(k))}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if value is None or isinstance(value, (int, float, bool)):
            return value
        return self.text(str(value))
