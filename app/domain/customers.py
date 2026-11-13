"""Customer identifiers: shared canonicalization, no conversation intent parsing."""

import re
import unicodedata


def normalize_nickname(value: str | None) -> str | None:
    if not value:
        return None
    plain = "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", plain).strip().upper() or None


def suggested_nickname(name: str | None) -> str | None:
    first = (normalize_nickname(name) or "").split(" ")[0]
    letters = "".join(c for c in first if c.isalpha())
    return letters[:3] if len(letters) >= 3 else None


def is_nickname_reference(value: str) -> bool:
    normalized = normalize_nickname(value) or ""
    return len(normalized) == 3 and normalized.isalpha()
