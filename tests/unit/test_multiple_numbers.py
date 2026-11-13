"""Authentication and immutable account selection, without outbound network traffic."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.application.choices import Choice
from app.channels.kapso.provider import KapsoProvider, compute_signature
from app.channels.types import OutboundInteractive
from app.config import Settings
from app.container import build_messaging
from tests.api.test_api import kapso_event


def provider(http=None):
    return KapsoProvider(api_key="primary-key", phone_number_id="123", webhook_secret="primary-secret",
                         additional_numbers={"456": ("backup-key", "backup-secret")},
                         base_url="https://kapso.invalid", api_version="v24.0", http=http)


@pytest.mark.parametrize(("account", "secret", "accepted"), [
    ("123", "primary-secret", True), ("456", "backup-secret", True),
    ("123", "backup-secret", False), ("456", "primary-secret", False),
    ("999", "primary-secret", False), (None, "primary-secret", False),
])
def test_signature_binds_the_whole_body_to_the_receiving_number(account, secret, accepted):
    event = kapso_event("wamid.in", "hola")
    if account is None:
        event.pop("phone_number_id")
    else:
        event["phone_number_id"] = account
    raw = json.dumps(event).encode()
    assert provider().verify_signature(raw, {"x-webhook-signature": compute_signature(secret, raw)}) is accepted


@pytest.mark.parametrize("payload", [[], None, {"batch": True, "data": []},
    {"batch": True, "data": [{"phone_number_id": "123"}, {"phone_number_id": "456"}]},
    {"batch": True, "data": [None]}, {"batch": True, "data": None}])
def test_invalid_and_mixed_account_batches_are_rejected(payload):
    raw = json.dumps(payload).encode()
    assert not provider().verify_signature(raw, {"x-webhook-signature": compute_signature("primary-secret", raw)})


async def test_all_send_operations_stay_bound_during_concurrent_work():
    seen = []

    async def send(request):
        await asyncio.sleep(0)  # interleave both accounts before reading their credentials/URL
        seen.append((request.url.path, request.headers["x-api-key"], json.loads(request.content)))
        return httpx.Response(200, json={"messages": [{"id": f"out-{len(seen)}"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
        root = provider(http)

        async def exercise(account):
            bound = root.for_phone_number(account)
            await bound.send_text(account, "hola")
            await bound.send_image(account, "https://image.invalid/test.png", "imagen")
            await bound.send_interactive(account, OutboundInteractive("elige", (Choice("a", "Sí"),)))
            await bound.react(account, f"in-{account}", "👍")
            await bound.show_typing(f"in-{account}")
            await bound.mark_read(f"in-{account}")
            await bound.send_contact_request(account, "comparte tu contacto")

        await asyncio.gather(exercise("123"), exercise("456"))
        await root.send_text("default", "principal")
    assert len(seen) == 15
    for path, key, body in seen:
        recipient = body.get("to") or body.get("recipient") or body["message_id"].removeprefix("in-")
        account = "123" if recipient == "default" else recipient
        assert path == f"/v24.0/{account}/messages"
        assert key == ("primary-key" if account == "123" else "backup-key")


def test_unknown_account_never_falls_back_to_primary():
    with pytest.raises(ValueError, match="no configurado"):
        provider().for_phone_number("999")


def test_additional_numbers_are_loaded_from_json_environment(monkeypatch):
    monkeypatch.setenv("KAPSO_ADDITIONAL_NUMBERS", '{"456":{"webhook_secret":"backup-secret"}}')
    settings = Settings(database_url="postgresql+asyncpg://unused", redis_url="redis://unused", messaging_provider="kapso",
                        kapso_phone_number_id="123", kapso_api_key="primary-key", kapso_webhook_secret="primary-secret")
    root = build_messaging(settings)
    backup = root.for_phone_number("456")
    assert backup._api_key == "primary-key" and backup._secret == "backup-secret"


@pytest.mark.parametrize("numbers", [
    {"123": {"webhook_secret": "secret"}}, {"invalid/id": {"webhook_secret": "secret"}},
    {"456": {"webhook_secret": ""}}, {"456": {"api_key": "key"}},
    {"456": {"webhook_secret": "secret", "unexpected": "bad"}},
])
def test_invalid_number_configuration_is_rejected(numbers):
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+asyncpg://unused", redis_url="redis://unused",
                 kapso_phone_number_id="123", kapso_api_key="key", kapso_webhook_secret="secret",
                 kapso_additional_numbers=numbers)
