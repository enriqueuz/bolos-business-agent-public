"""Fixtures sintéticas: no son los webhooks originales del incidente D06."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.channels.kapso import provider
from app.channels.kapso.provider import parse_message_event

FIXTURE = Path(__file__).parents[1] / "fixtures" / "kapso_unavailable.json"


@pytest.mark.parametrize("code", [131060, "131060"])
def test_unavailable_code_preserves_diagnostics_and_citation_without_content(code):
    event = json.loads(FIXTURE.read_text())
    event["message"]["errors"][0]["code"] = code
    event["message"]["kapso"].pop("content")
    event["message"]["context"] = {"id": "wamid.receipt"}
    msg = parse_message_event(event)
    assert msg.type == "unavailable" and msg.text == "Mensaje no disponible · WhatsApp 131060"
    assert msg.raw == event and msg.provider_message_id == "wamid.fixture.131060"
    assert msg.reply_to_provider_message_id == "wamid.receipt"


def test_placeholder_is_recognized_when_kapso_omits_structured_errors():
    event = json.loads(FIXTURE.read_text())
    del event["message"]["errors"]
    assert parse_message_event(event).type == "unavailable"


def test_error_takes_priority_over_declared_text_type():
    event = json.loads(FIXTURE.read_text())
    event["message"].update(type="text", text={"body": "not actual content"})
    assert parse_message_event(event).type == "unavailable"


@pytest.mark.parametrize("content", [None, "[Unsupported message - Error 131051]: Unsupported message type."])
def test_other_unsupported_messages_keep_their_independent_behavior(content):
    event = json.loads(FIXTURE.read_text())
    event["message"]["errors"][0]["code"] = 131051
    event["message"]["kapso"]["content"] = content
    msg = parse_message_event(event)
    assert msg.type == "unsupported" and msg.text == content


def test_normal_text_mentioning_the_error_is_not_reclassified():
    event = json.loads(FIXTURE.read_text())
    del event["message"]["errors"]
    event["message"].update(type="text", text={"body": "¿Qué significa error 131060?"})
    assert parse_message_event(event).type == "text"
    assert parse_message_event(event).text == "¿Qué significa error 131060?"


def test_outbound_unavailable_event_is_ignored():
    event = json.loads(FIXTURE.read_text())
    event["message"]["kapso"]["direction"] = "outbound"
    assert parse_message_event(event) is None


def test_structured_log_contains_only_diagnostic_identifiers(monkeypatch):
    recorded = []
    monkeypatch.setattr(provider, "log", SimpleNamespace(warning=lambda event, **fields: recorded.append((event, fields))))
    event = json.loads(FIXTURE.read_text())
    parse_message_event(event)
    assert recorded == [("kapso.message_unavailable", {
        "error_code": 131060, "provider_message_id": "wamid.fixture.131060", "phone_number_id": "123",
        "source": "message.errors",
    })]
