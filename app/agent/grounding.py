"""Cifras con origen: cada número del texto del modelo debe salir de los resultados de las tools del turno,
del mensaje del dueño, de los recibos, o derivarse de ellos (suma, diferencia o porcentaje de dos valores).

No es un verificador perfecto: es una red barata y determinista contra la falla más cara en BI conversacional,
que es un número inventado dicho con seguridad.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from app.agent.receipts import has_receipt_header
from app.domain.text import extract_numbers

MAX_DERIVATION_NUMBERS = 80


@dataclass(slots=True)
class GroundingReport:
    ok: bool
    ungrounded: list[float] = field(default_factory=list)
    checked: int = 0
    claimed_write: bool = False  # afirmó haber escrito algo y el turno no escribió nada
    action_check_failed: bool = False
    presentation_violation: bool = False

    @property
    def failure_reasons(self) -> list[str]:
        reasons = []
        if self.ungrounded:
            reasons.append("unsupported_figures")
        if self.claimed_write:
            reasons.append("unverified_write_claim")
        if self.action_check_failed:
            reasons.append("action_verification_failed")
        if self.presentation_violation:
            reasons.append("presentation_rule_violation")
        return reasons

    def failure_message(self) -> str:
        return f"Grounding rejected response: {', '.join(self.failure_reasons)}; figures={self.ungrounded}"

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "ungrounded": self.ungrounded, "checked": self.checked,
                "claimed_write": self.claimed_write, "action_check_failed": self.action_check_failed,
                "presentation_violation": self.presentation_violation,
                "failure_reasons": self.failure_reasons}


def _collect(obj: Any, out: list[float]) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            _collect(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect(v, out)
    elif isinstance(obj, bool):
        return
    elif isinstance(obj, int | float):
        out.append(abs(float(obj)))
    elif isinstance(obj, str):
        out.extend(extract_numbers(obj))


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.011, abs(b) * 0.006)


def _ignorable(n: float) -> bool:
    return (n.is_integer() and n <= 31) or (2020 <= n <= 2035 and n.is_integer())


def check(model_text: str, tool_results: list[Any], user_text: str = "", receipts: list[str] | None = None,
          wrote_something: bool = True, *, claims_current_turn_write: bool = False,
          action_check_failed: bool = False) -> GroundingReport:
    # Free-language claims are classified by the model, never by substrings.
    # Receipt titles remain an exact syntactic signal backed by command results.
    claimed = bool(model_text.strip()) and not wrote_something and (
        claims_current_turn_write or has_receipt_header(model_text))
    candidates = [n for n in extract_numbers(model_text) if not _ignorable(n)]
    if not candidates:
        return GroundingReport(not claimed and not action_check_failed, [], 0,
                               claimed_write=claimed, action_check_failed=action_check_failed)
    base: list[float] = []
    for r in tool_results:
        _collect(r if not isinstance(r, str) else json.loads(r) if r.startswith("{") else r, base)
    base.extend(extract_numbers(user_text))
    for rc in receipts or []:
        base.extend(extract_numbers(rc))
    base = list(dict.fromkeys(round(b, 4) for b in base))

    derived: set[float] = set()
    # Bound quadratic derivations, not the direct evidence. Dates and metadata in
    # long tool results must never push an actual amount out of the source pool.
    for a, b in combinations(base[:MAX_DERIVATION_NUMBERS], 2):
        derived.update({round(a + b, 4), round(abs(a - b), 4)})
        if b:
            derived.add(round(a / b * 100, 2))
            derived.add(round(abs((a - b) / b * 100), 2))
            derived.add(round(a / b, 4))
        if a:
            derived.add(round(b / a * 100, 2))
            derived.add(round(abs((b - a) / a * 100), 2))
            derived.add(round(b / a, 4))
    pool = base + list(derived)
    ungrounded = [n for n in candidates if not any(_close(n, p) for p in pool)]
    return GroundingReport(not ungrounded and not claimed and not action_check_failed, ungrounded, len(candidates),
                           claimed_write=claimed, action_check_failed=action_check_failed)
