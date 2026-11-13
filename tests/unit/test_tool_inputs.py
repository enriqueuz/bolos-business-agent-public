"""Contratos de normalización para argumentos producidos por el modelo."""

import pytest
from pydantic import TypeAdapter

from app.agent.tool_inputs import NullableToolString


@pytest.mark.parametrize("raw", ["null", "NULL", "  Null  "])
def test_nullable_tool_string_normalizes_textual_null(raw: str) -> None:
    assert TypeAdapter(NullableToolString).validate_python(raw) is None


@pytest.mark.parametrize("raw", [None, "Carlos", "nullifier", ""])
def test_nullable_tool_string_preserves_other_values(raw: str | None) -> None:
    assert TypeAdapter(NullableToolString).validate_python(raw) == raw


def test_nullable_tool_string_keeps_nullable_json_schema() -> None:
    assert TypeAdapter(NullableToolString).json_schema() == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
    }
