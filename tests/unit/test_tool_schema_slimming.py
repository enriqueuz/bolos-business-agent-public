"""El schema de cada tool viaja adelgazado al modelo (ítem C1 de docs/evaluation.md).

El schema se manda ENTERO en cada llamada y un turno del carril agente hace dos, así que todo lo que sobra se
paga dos veces por turno. `pydantic_ai` lo entrega al proveedor sin transformar
(`_function_declaration_from_tool`), de modo que lo que se quita aquí es lo que se deja de pagar.

Lo que NO debe cambiar es qué acepta cada tool: estos tests fijan esa frontera.
"""

from __future__ import annotations

import json

from app.agent.tools.registry import TOOLS, build_toolset, slim_schema
from app.domain.enums import Lane


def _schemas() -> dict[str, dict]:
    ts = build_toolset()
    return {name: tool.tool_def.parameters_json_schema for name, tool in ts.tools.items()}


def test_titles_are_dropped() -> None:
    """`title` es un metadato que el modelo no usa: solo ocupa tokens."""
    for name, schema in _schemas().items():
        assert "title" not in json.dumps(slim_schema(schema)), f"{name} conserva algún title"


def test_internal_docstrings_of_enums_are_dropped() -> None:
    """Las descripciones de los `$defs` son docstrings para desarrolladores, no instrucciones para el modelo.

    Alguna llega a citar un archivo del repo ("lo decide su perfil (countries.py)"), que al modelo no le dice nada.
    """
    for name, schema in _schemas().items():
        for def_name, definition in slim_schema(schema).get("$defs", {}).items():
            assert "description" not in definition, f"{name}.{def_name} conserva su docstring interno"


def test_optional_fields_keep_explicit_nullability_and_description() -> None:
    sale = slim_schema(_schemas()["record_sale"])
    customer = sale["properties"]["customer_name"]
    assert customer == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
        "description": "Cliente, obligatorio si es fiado o parcial.",
    }
    assert "customer_name" not in sale["required"]


def test_broker_name_keeps_null_as_a_real_json_value() -> None:
    operation = slim_schema(_schemas()["record_broker_operation"])
    broker_name = operation["properties"]["broker_name"]
    assert broker_name["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert broker_name["default"] is None


def test_purchase_settlement_confirmation_is_never_a_model_argument() -> None:
    for name, schema in _schemas().items():
        assert "purchase_settlement_confirmed" not in schema["properties"], name
        assert "draft_origin_command_kind" not in schema["properties"], name
        assert "require_purchase_settlement_choice" not in schema["properties"], name


def test_broker_account_payment_has_no_direction_and_generic_movement_tool_is_gone() -> None:
    schemas = _schemas()
    assert "record_broker_movement" not in schemas
    assert "record_broker_entry" not in schemas
    assert "direction" not in schemas["record_broker_account_payment"]["properties"]
    withdrawal = schemas["record_broker_account_withdrawal"]["properties"]
    assert "amount" in withdrawal
    assert not {"direction", "customer_name", "operation_amount_bs", "exchange_rate", "draft_operation_id"} & withdrawal.keys()


def test_refs_are_left_alone() -> None:
    """Colapsar un `$ref` puede hacer que se pierdan sus hermanos (la description del parámetro) al resolverlo."""
    paid_currency = slim_schema(_schemas()["record_sale"])["properties"]["paid_currency"]
    assert paid_currency["anyOf"] == [{"$ref": "#/$defs/Currency"}, {"type": "null"}]
    assert paid_currency["description"].startswith("Moneda en que pagó")


def test_meaningful_defaults_survive() -> None:
    """Los defaults son parte del contrato que orienta al proveedor, incluido `null`."""
    sale = slim_schema(_schemas()["record_sale"])
    assert sale["properties"]["payment_status"]["default"] == "paid"
    assert sale["properties"]["force_new_customer"]["default"] is False


def test_nothing_required_becomes_optional_or_viceversa() -> None:
    """La frontera de lo que cada tool acepta no se mueve: adelgazar no es cambiar el contrato."""
    for name, schema in _schemas().items():
        assert slim_schema(schema).get("required") == schema.get("required"), f"{name} cambió sus obligatorios"
        assert set(slim_schema(schema)["properties"]) == set(schema["properties"]), f"{name} cambió sus campos"


def test_slimming_saves_a_meaningful_share_of_the_schema() -> None:
    """Quita metadatos sin volver a borrar nulabilidad semánticamente relevante para ahorrar tokens."""
    schemas = _schemas()
    agent = [s for n, s in schemas.items() if Lane.AGENT in TOOLS[n].lanes]
    before = sum(len(json.dumps(s, ensure_ascii=False)) for s in agent)
    after = sum(len(json.dumps(slim_schema(s), ensure_ascii=False)) for s in agent)
    assert (before - after) / before > 0.05, f"el ahorro cayó a {100 * (before - after) / before:.1f}%"
