"""La memoria de corto plazo que se le arma al modelo (`history_from_window`).

Lo que protege esto es una sola cosa, y costó un bug en producción: **lo que hizo el SISTEMA nunca va dentro de la
respuesta del asistente**. Metido ahí, el modelo lee que «él» cerró sus turnos anteriores con un corchete de
metadatos y copia el patrón — se le coló el corchete al dueño, y empezó a imitar las preguntas del sistema con
`ask_choice` en vez de llamar a la herramienta (la venta no quedaba registrada).
"""

from __future__ import annotations

from pydantic_ai.messages import ModelRequest, ModelResponse

from app.agent.context_builder import history_from_window
from app.application.conversation import WindowEntry


def _texts(messages, kind) -> list[str]:
    return [p.content for m in messages if isinstance(m, kind) for p in m.parts if hasattr(p, "content")]


def test_system_actions_never_land_inside_the_assistant_message():
    msgs = history_from_window([WindowEntry(user="vendí 2 bujías a 2$", assistant="✅ *Venta registrada*",
                                            actions=["record_sale elegido"])])
    said = _texts(msgs, ModelResponse)
    assert said == ["✅ *Venta registrada*"]
    assert not any("acciones del sistema" in t for t in said)


def test_the_actions_are_still_visible_to_the_model():
    """Sacarlas de la voz del asistente no es esconderlas: el modelo necesita saber que el dueño tocó un botón."""
    msgs = history_from_window([WindowEntry(user="vendí 2 bujías a 2$", assistant="✅ *Venta registrada*",
                                            actions=["record_sale elegido"])])
    assert any("record_sale elegido" in t for t in _texts(msgs, ModelRequest))


def test_a_turn_without_actions_adds_no_extra_message():
    msgs = history_from_window([WindowEntry(user="hola", assistant="¡Hola!")])
    assert len(msgs) == 2
    assert _texts(msgs, ModelResponse) == ["¡Hola!"]


def test_empty_turns_keep_the_alternation_readable():
    """Un turno sin texto (una foto, una reacción) no puede dejar un mensaje vacío: algunos proveedores los rechazan."""
    msgs = history_from_window([WindowEntry(user="", assistant="")])
    assert _texts(msgs, ModelRequest) == ["(mensaje sin texto)"]
    assert _texts(msgs, ModelResponse) == ["(sin respuesta)"]
