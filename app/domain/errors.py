"""Errores de dominio tipados. Se traducen a JSON para el agente y a HTTP para el panel."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    code: str = "domain_error"
    http_status: int = 422

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": self.message, **self.details}


class ValidationFailed(DomainError):
    code = "invalid"


class MissingBrokerBolivars(ValidationFailed):
    code = "missing_bolivars"

    def __init__(self) -> None:
        super().__init__("Dime la cantidad de bolívares o envíame el capture del pago.")


class MissingBrokerPurchaseCustomer(ValidationFailed):
    code = "missing_purchase_customer"

    def __init__(self) -> None:
        super().__init__("¿Qué cliente quedó debiendo esta compra?")


class NotFound(DomainError):
    code = "not_found"
    http_status = 404


class Ambiguous(DomainError):
    """Varias coincidencias posibles: el agente debe preguntar o repetir con el nombre exacto."""

    code = "ambiguous"
    http_status = 409


class Stale(DomainError):
    """El registro cambió desde que se leyó (bloqueo optimista)."""

    code = "stale"
    http_status = 409


class Conflict(DomainError):
    code = "conflict"
    http_status = 409


class NeedsConfirmation(DomainError):
    """Panel: la acción parece un error (p. ej. una tasa muy lejos de la oficial). Se reenvía con ?confirmed=true si el dueño dice que sí."""

    code = "needs_confirmation"
    http_status = 409


class NeedsChoice(DomainError):
    """El comando no puede seguir sin que el dueño elija entre opciones concretas (botones o lista).

    Cada opción es un dict con `title`, `description` y `action`: "command" (el mismo comando u otro, ya armado, en
    `command_kind` + `command_payload`, que se ejecuta sin LLM al tocarla) o "reply" (el título vuelve al agente como la
    respuesta del dueño). El panel recibe lo mismo en el 409 y reenvía el payload elegido.
    """

    code = "needs_choice"
    http_status = 409

    def __init__(self, question: str, options: list[dict[str, Any]]) -> None:
        super().__init__(question, options=options)

    @property
    def options(self) -> list[dict[str, Any]]:
        return self.details["options"]


class RatesUnavailable(DomainError):
    code = "rates_unavailable"
    http_status = 503
