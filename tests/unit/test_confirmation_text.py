"""El sí/no de una confirmación: estricto. Un falso "no entendí" cuesta repetir; un falso "sí" ejecuta algo no aprobado."""

import pytest

from app.domain.text import is_affirmative, is_negative

YES = [
    "sí", "Si", "SÍ!!", "siii", "sí, hazlo", "sí por favor", "si dale", "sí dale pues", "sí claro", "sí señor", "sí gracias",
    "dale", "dale pues", "daleee", "de una", "hecho", "yes", "ok", "ok gracias", "va pues", "👍", "👌", "✅", "confirmo",
    "correcto", "exacto", "así es", "de acuerdo", "por supuesto", "claro que sí", "sí, confirma", "sí confírmalo",
]
NO = ["no", "No.", "nooo", "no gracias", "no, gracias", "no, cancela", "no cancela", "mejor no", "no lo hagas", "cancela",
      "cancélalo", "déjalo", "olvídalo", "nop", "nel", "❌", "👎", "para", "no no"]
# Nada de esto puede confirmar ni cancelar por sí mismo: pasa al LLM (y la confirmación pendiente se descarta).
NEITHER = [
    # mezclas peligrosas: una parte dice sí, otra cambia o niega
    "sí pero cámbialo a 20", "sí, pero mejor 25 dólares", "sí y agrega una venta de 5$", "sí hazlo por 30", "no, sí", "sí no",
    "si no", "sí... no sé", "claro que no", "eso no", "bueno no", "ok pero espera", "dale pero a Ana", "no, mejor sí",
    # preguntas: el dueño está dudando, no respondiendo
    "si?", "¿sí?", "¿dale?", "¿seguro?", "sí, ¿cuánto era?", "ok?",
    # otros mensajes
    "¿cuánto vendí hoy?", "vendí 2 bujías a 3$", "borra la venta de ayer", "gracias", "hola", "", "   ", "sí " * 6,
]


@pytest.mark.parametrize("text", YES)
def test_recognizes_a_yes(text):
    assert is_affirmative(text) and not is_negative(text), text


@pytest.mark.parametrize("text", NO)
def test_recognizes_a_no(text):
    assert is_negative(text) and not is_affirmative(text), text


@pytest.mark.parametrize("text", NEITHER)
def test_everything_else_is_neither_yes_nor_no(text):
    assert not is_affirmative(text) and not is_negative(text), text
