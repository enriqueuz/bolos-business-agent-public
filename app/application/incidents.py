"""Provider-neutral error reporting. Reporting must never change a business outcome."""
from __future__ import annotations

from typing import Any, Protocol


class IncidentReporter(Protocol):
    async def report(self, exc: Exception, *, context: dict[str, Any]) -> None: ...


class NullIncidentReporter:
    async def report(self, exc: Exception, *, context: dict[str, Any]) -> None:
        pass
