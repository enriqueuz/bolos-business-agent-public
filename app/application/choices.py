"""Opciones tocables (botones y listas de WhatsApp), en términos neutrales del sistema.

El id de cada opción lo define Bolos y vuelve tal cual cuando el dueño la toca. Formatos:
  c:<token>:yes | c:<token>:no   confirmación de una acción congelada (PendingAction.token)
  o:<token>:<n>                  opción n (1..10) de una elección pendiente (PendingChoice.token)
  u:<token>                      deshacer los registros de un recibo (UndoEntry.token)
  onb:<campo>:<valor>            respuesta del onboarding

Los tokens son de un solo uso: tocar un botón viejo (otra confirmación, ya respondida o caducada) no ejecuta nada.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

CONFIRM, OPTION, UNDO, ONBOARDING = "c", "o", "u", "onb"
YES, NO = "yes", "no"


def new_token() -> str:
    return secrets.token_urlsafe(6)


@dataclass(frozen=True, slots=True)
class Choice:
    """Una opción tocable. Con ≤3 opciones sin descripción se muestra como botones; si no, como lista."""

    id: str
    title: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ButtonPress:
    kind: str  # c | o | u | onb
    token: str  # en onboarding: el campo
    value: str  # yes/no, número de opción, "" en deshacer, o el valor del onboarding


def parse_button(button_id: str | None) -> ButtonPress | None:
    if not button_id:
        return None
    parts = button_id.split(":")
    if parts[0] in (CONFIRM, OPTION, ONBOARDING) and len(parts) == 3 and all(parts):
        return ButtonPress(parts[0], parts[1], parts[2])
    if parts[0] == UNDO and len(parts) == 2 and parts[1]:
        return ButtonPress(UNDO, parts[1], "")
    return None


def confirmation_choices(token: str) -> tuple[Choice, ...]:
    return (Choice(f"{CONFIRM}:{token}:{YES}", "Sí"), Choice(f"{CONFIRM}:{token}:{NO}", "No"))


def option_id(token: str, n: int) -> str:
    return f"{OPTION}:{token}:{n}"


def undo_choice(token: str) -> Choice:
    return Choice(f"{UNDO}:{token}", "Deshacer")


def onboarding_id(field: str, value: str) -> str:
    return f"{ONBOARDING}:{field}:{value}"
