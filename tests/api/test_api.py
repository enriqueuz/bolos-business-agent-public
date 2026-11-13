"""API HTTP: webhook de Kapso (firma, lotes, dedup, cola) y panel (auth OTP, CRUD vía CommandBus, bloqueo optimista)."""

from __future__ import annotations

import asyncio
import io
import json
import os
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from arq import create_pool
from arq.connections import RedisSettings
from PIL import Image
from sqlalchemy import func, select

from app.api.panel.auth import hash_password
from app.channels.kapso.provider import KapsoProvider, compute_signature
from app.config import Settings
from app.container import build_container
from app.db.models import (
    Business,
    BusinessMember,
    Customer,
    InboundMessage,
    Product,
    WhatsAppIdentity,
)
from app.domain.enums import BusinessVertical, OnboardingState
from app.main import create_app
from tests.agent.harness import FakeRates, Script, build_test_container

SECRET = "kapso-test-secret"


def kapso_event(message_id: str, body: str, phone: str = "584140000001", context_id: str | None = None) -> dict:
    msg = {"id": message_id, "timestamp": "1730092800", "type": "text", "from": phone, "text": {"body": body},
           "kapso": {"direction": "inbound", "status": "received", "content": body}}
    if context_id:
        msg["context"] = {"from": "584120000000", "id": context_id}
    return {"message": msg, "conversation": {"id": "conv_1", "contact_name": "Enrique", "phone_number": phone,
                                             "phone_number_id": "123"}, "is_new_conversation": False, "phone_number_id": "123"}


@pytest_asyncio.fixture
async def kapso_client(redis, session_factory):
    settings = Settings(database_url=os.environ["DATABASE_URL"], redis_url=os.environ["REDIS_URL"], app_env="test",
                        messaging_provider="kapso", kapso_api_key="k", kapso_phone_number_id="123", kapso_webhook_secret=SECRET)
    provider = KapsoProvider(api_key="k", phone_number_id="123", webhook_secret=SECRET, base_url="http://kapso.invalid",
                             api_version="v24.0", platform_base_url="http://kapso.invalid/platform/v1")

    async def unknown_contact(bsuid: str) -> None:  # sin red: el test que lo necesite lo reemplaza
        return None

    provider.lookup_phone = unknown_contact
    container = build_container(settings, messaging=provider, rate_fetchers={"dolarapi_ve": FakeRates()}, roles=build_test_container(Script())[0].roles)
    app = create_app(settings, container)
    app.state.container = container
    app.state.arq = await create_pool(RedisSettings.from_dsn(os.environ["REDIS_URL"]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, container
    await app.state.arq.aclose()
    await container.close()


async def post_signed(client, payload: dict, headers: dict | None = None, secret: str = SECRET):
    raw = json.dumps(payload).encode()
    return await client.post("/webhooks/whatsapp", content=raw, headers={
        "content-type": "application/json", "x-webhook-signature": compute_signature(secret, raw),
        "x-webhook-event": "whatsapp.message.received", **(headers or {})})


async def test_webhook_rejects_bad_signature(kapso_client):
    client, _ = kapso_client
    resp = await post_signed(client, kapso_event("wamid.1", "hola"), secret="otro")
    assert resp.status_code == 401


async def test_webhook_dedups_and_enqueues(kapso_client, session_factory, redis):
    client, _ = kapso_client
    first = await post_signed(client, kapso_event("wamid.A", "vendí 2 bujías", context_id="wamid.OUT"))
    again = await post_signed(client, kapso_event("wamid.A", "vendí 2 bujías"))
    assert first.json()["queued"] == 1 and again.json()["queued"] == 0
    async with session_factory() as s:
        msg = (await s.execute(select(InboundMessage))).scalar_one()
        assert msg.reply_to_provider_message_id == "wamid.OUT" and msg.body == "vendí 2 bujías"
    assert await redis.exists("arq:queue")


async def test_webhook_batch_payload(kapso_client, session_factory):
    client, _ = kapso_client
    batch = {"type": "whatsapp.message.received", "batch": True,
             "data": [kapso_event("wamid.B1", "uno"), kapso_event("wamid.B2", "dos")], "batch_info": {"size": 2}}
    resp = await post_signed(client, batch, headers={"x-webhook-batch": "true"})
    assert resp.json()["queued"] == 2
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(InboundMessage))).scalar_one() == 2


# ------------------------------------------------------------------------------------------------ usernames (BSUID)

BSUID = "VE.13491208655302741918"


def bsuid_event(message_id: str, body: str | None = "hola", phone: str | None = None, **message) -> dict:
    event = kapso_event(message_id, body or "", phone=phone or "")
    event["message"].pop("from")
    event["message"].update({"from_user_id": BSUID, **({"from": phone} if phone else {}), **message})
    event["conversation"].update({"phone_number": phone, "business_scoped_user_id": BSUID})
    return event


async def inbound_by_mid(session_factory, mid: str) -> InboundMessage:
    async with session_factory() as s:
        return (await s.execute(select(InboundMessage).where(InboundMessage.provider_message_id == mid))).scalar_one()


async def test_webhook_learns_the_bsuid_and_resolves_it_later(kapso_client, session_factory):
    client, _ = kapso_client
    await post_signed(client, bsuid_event("wamid.P1", phone="584140000001"))
    async with session_factory() as s:
        row = (await s.execute(select(WhatsAppIdentity))).scalar_one()
        assert (row.bsuid, row.phone, row.source) == (BSUID, "584140000001", "webhook")
    assert (await post_signed(client, bsuid_event("wamid.P2", "Cafe 6000 pesos"))).json()["queued"] == 1
    msg = await inbound_by_mid(session_factory, "wamid.P2")
    assert (msg.from_phone, msg.from_bsuid) == ("584140000001", BSUID)


async def test_webhook_asks_kapso_contacts_for_an_unknown_bsuid(kapso_client, session_factory, monkeypatch):
    client, c = kapso_client

    async def kapso_knows(bsuid):
        return "584140000001" if bsuid == BSUID else None

    monkeypatch.setattr(c.messaging, "lookup_phone", kapso_knows)
    await post_signed(client, bsuid_event("wamid.K1"))
    assert (await inbound_by_mid(session_factory, "wamid.K1")).from_phone == "584140000001"
    async with session_factory() as s:
        assert (await s.execute(select(WhatsAppIdentity.source))).scalar_one() == "kapso"


async def test_webhook_never_drops_an_unknown_bsuid(kapso_client, session_factory):
    client, _ = kapso_client
    assert (await post_signed(client, bsuid_event("wamid.U1", "Cafe 6000 pesos"))).json()["queued"] == 1
    msg = await inbound_by_mid(session_factory, "wamid.U1")
    assert (msg.from_phone, msg.from_bsuid, msg.body) == (None, BSUID, "Cafe 6000 pesos")


async def test_webhook_contact_shared_replays_parked_messages(kapso_client, session_factory, business, redis):
    client, c = kapso_client
    await post_signed(client, bsuid_event("wamid.W1", "Cafe 6000 pesos"))
    parked = await inbound_by_mid(session_factory, "wamid.W1")
    await c.turns.start_turn(business_id=None, inbound_message_id=parked.id)  # su turno ya pidió el contacto
    shared = bsuid_event("wamid.W2", None, type="contacts", contacts=[
        {"origin": "contact_request", "phones": [{"phone": "+58 414-000-0001", "wa_id": "584140000001"}]}])
    del shared["message"]["text"]
    assert (await post_signed(client, shared)).json()["queued"] == 1
    assert (await inbound_by_mid(session_factory, "wamid.W1")).from_phone == "584140000001"
    assert (await inbound_by_mid(session_factory, "wamid.W2")).type == "contact_shared"
    assert await redis.exists(f"arq:job:inbound:{parked.id}:replay")
    async with session_factory() as s:
        assert (await s.execute(select(WhatsAppIdentity.source))).scalar_one() == "contact_request"


async def test_webhook_contact_shared_without_access_does_not_replay(kapso_client, session_factory, redis, monkeypatch):
    client, c = kapso_client
    monkeypatch.setattr(c.identity, "can_signup", lambda phone: False)  # acceso privado y el número no es dueño
    await post_signed(client, bsuid_event("wamid.N1"))
    parked = await inbound_by_mid(session_factory, "wamid.N1")
    await c.turns.start_turn(business_id=None, inbound_message_id=parked.id)
    shared = bsuid_event("wamid.N2", None, type="contacts", contacts=[
        {"origin": "contact_request", "phones": [{"wa_id": "584149999999"}]}])
    await post_signed(client, shared)
    assert not await redis.exists(f"arq:job:inbound:{parked.id}:replay")


def test_kapso_parser_voice_image_and_outbound_echo():
    from app.channels.kapso.provider import parse_message_event

    voice = parse_message_event({"message": {"id": "w1", "from": "58414", "type": "audio", "audio": {"mime_type": "audio/ogg"},
                                             "kapso": {"transcript": {"text": "vendí tres aceites"}, "media_url": "https://m"}}})
    assert voice.type == "voice" and voice.transcript == "vendí tres aceites"
    image = parse_message_event({"message": {"id": "w2", "from": "58414", "type": "image", "image": {"caption": "pago"},
                                             "kapso": {"media_url": "https://img"}}})
    assert image.type == "image" and image.media_url == "https://img" and image.text == "pago"
    assert parse_message_event({"message": {"id": "w3", "from": "58414", "type": "text", "text": {"body": "x"},
                                            "kapso": {"direction": "outbound"}}}) is None


# ------------------------------------------------------------------------------------------------ panel


@pytest_asyncio.fixture
async def panel_client(redis, session_factory):
    container, console = build_test_container(Script(), system_admin_phones="584140000001",
                                              system_admin_password_hash=hash_password("solo-para-test"))
    await container.rates.refresh()
    async with session_factory() as s, s.begin():
        b = Business(name="El Rayo", onboarding_state=OnboardingState.DONE.value, welcomed=True, payment_methods=["cash"])
        s.add(b)
        await s.flush()
        s.add(BusinessMember(business_id=b.id, phone="584140000001", name="Enrique"))
    app = create_app(container.settings, container)
    app.state.container = container
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        code = (await client.post("/api/panel/v1/auth/request-code", json={"phone": "+58 414 0000001"})).json()["dev_code"]
        assert "código" in console.sent[-1].text
        resp = await client.post("/api/panel/v1/auth/verify", json={"phone": "584140000001", "code": code})
        assert resp.status_code == 200
        yield client, container
    await container.close()


async def test_panel_requires_auth(redis, session_factory):
    container, _ = build_test_container(Script())
    app = create_app(container.settings, container)
    app.state.container = container
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/panel/v1/me")).status_code == 401
    await container.close()


async def test_panel_wrong_code_is_rejected(panel_client):
    client, _ = panel_client
    await client.post("/api/panel/v1/auth/request-code", json={"phone": "584140000001"})
    resp = await client.post("/api/panel/v1/auth/verify", json={"phone": "584140000001", "code": "000000"})
    assert resp.status_code in {401, 429}


async def test_the_welcome_link_opens_the_panel_once_and_never_again(redis, session_factory):
    """El enlace de bienvenida entra sin código, y se quema al usarse.

    Es la única URL de Bolos que es una credencial, así que el segundo toque —una captura del chat, un recibo
    reenviado, el mismo enlace mañana— tiene que valer lo mismo que no tener nada.
    """
    container, _ = build_test_container(Script())
    async with session_factory() as s, s.begin():
        b = Business(name="El Rayo", onboarding_state=OnboardingState.DONE.value, welcomed=True, payment_methods=["cash"])
        s.add(b)
        await s.flush()
        s.add(BusinessMember(business_id=b.id, phone="584140000001", name="Enrique"))
        business_id = b.id
    app = create_app(container.settings, container)
    app.state.container = container
    token = await container.links.mint("584140000001", business_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/panel/v1/me")).status_code == 401
        first = await client.post("/api/panel/v1/auth/link", json={"token": token})
        assert first.status_code == 200 and first.json()["business_id"] == str(business_id)
        assert (await client.get("/api/panel/v1/me")).status_code == 200  # ya hay sesión, sin pasar por el código
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as other:
        assert (await other.post("/api/panel/v1/auth/link", json={"token": token})).status_code == 401
    await container.close()


async def test_an_invented_link_token_or_one_of_a_phone_without_business_opens_nothing(redis, session_factory):
    from uuid import uuid4

    container, _ = build_test_container(Script())
    app = create_app(container.settings, container)
    app.state.container = container
    # Un token real, pero de un teléfono que no tiene negocio: el token no basta, la identidad tiene que existir.
    orphan = await container.links.mint("584149999999", uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for token in ("inventado", orphan):
            assert (await client.post("/api/panel/v1/auth/link", json={"token": token})).status_code == 401
        assert (await client.get("/api/panel/v1/me")).status_code == 401
    await container.close()


async def test_panel_login_bootstraps_first_business_for_allowed_phone(redis, session_factory):
    """Base vacía + teléfono permitido: el panel debe poder crear el PRIMER negocio, igual que WhatsApp.

    Regresión: request_code usaba member_by_phone (solo lectura) y nunca daba código a un teléfono sin
    negocio previo — con la base vacía, nadie podía entrar al panel ni siquiera para el negocio piloto.
    """
    container, console = build_test_container(Script(), allowed_phones="584149990001")
    app = create_app(container.settings, container)
    app.state.container = container
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        req = await client.post("/api/panel/v1/auth/request-code", json={"phone": "584149990001"})
        assert req.status_code == 200
        code = req.json()["dev_code"]  # solo viene si de verdad se creó la identidad y se "envió" el código
        resp = await client.post("/api/panel/v1/auth/verify", json={"phone": "584149990001", "code": code})
        assert resp.status_code == 200
        me = (await client.get("/api/panel/v1/me")).json()
        assert me["business"]["onboarding_state"] == "in_progress"
        assert me["business"]["name"] is None
    await container.close()


async def test_panel_login_blocked_for_phone_outside_allowlist(redis, session_factory):
    container, _ = build_test_container(Script(), allowed_phones="584140000001")
    app = create_app(container.settings, container)
    app.state.container = container
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        req = await client.post("/api/panel/v1/auth/request-code", json={"phone": "584149999999"})
        assert req.status_code == 200 and "dev_code" not in req.json()  # respuesta genérica, no crea nada
        resp = await client.post("/api/panel/v1/auth/verify", json={"phone": "584149999999", "code": "000000"})
        assert resp.status_code == 401
    await container.close()


async def test_panel_crud_flow_with_optimistic_locking(panel_client):
    client, _ = panel_client
    sale = await client.post("/api/panel/v1/sales", json={
        "items": [{"product": "Bujía NGK", "quantity": "2", "unit_price": "3", "currency": "USD"}],
        "customer_name": "Moisés", "payment_status": "credit"})
    assert sale.status_code == 200, sale.text  # el panel no pide confirmación
    ref = sale.json()["affected"][0]
    movs = (await client.get("/api/panel/v1/movements")).json()
    assert movs["total"] == 1 and movs["items"][0]["money"]["amount"] == "6.0000"
    detail = (await client.get(f"/api/panel/v1/records/sale/{ref['entity_id']}")).json()
    assert detail["items"][0]["description"] == "Bujía NGK" and detail["history"][0]["actor"] == "panel"
    ok = await client.patch(f"/api/panel/v1/records/sale/{ref['entity_id']}",
                            json={"expected_version": ref["version"], "changes": {"quantity": "3"}})
    assert ok.status_code == 200
    stale = await client.patch(f"/api/panel/v1/records/sale/{ref['entity_id']}",
                               json={"expected_version": ref["version"], "changes": {"quantity": "4"}})
    assert stale.status_code == 409 and stale.json()["error"] == "stale"
    customers = (await client.get("/api/panel/v1/customers")).json()
    assert customers[0]["balance"]["owed"] == {"USD": "9.0000"}
    dash = (await client.get("/api/panel/v1/dashboard")).json()
    assert dash["today"]["sales"]["count"] == 1 and dash["receivables"]["customers_with_debt"] == 1


async def test_broker_abbreviation_defaults_to_initials_and_can_be_edited(panel_client):
    client, container = panel_client
    async with container.session_factory() as session, session.begin():
        business = (await session.execute(select(Business))).scalar_one()
        business.vertical = BusinessVertical.BROKERAGE.value

    created = await client.post("/api/panel/v1/brokerage/members", json={
        "name": "Alex Example", "phone": "584140000099",
    })
    assert created.status_code == 200, created.text
    assert created.json()["abbreviation"] == "AE"

    duplicate = await client.post("/api/panel/v1/brokerage/members", json={
        "name": "Ana Example", "phone": "584140000098",
    })
    assert duplicate.status_code == 409

    member_id = created.json()["id"]
    changed = await client.patch(
        f"/api/panel/v1/brokerage/members/{member_id}/abbreviation",
        json={"abbreviation": " sam.b "},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["abbreviation"] == "SAMB"
    members = (await client.get("/api/panel/v1/brokerage/members")).json()
    assert members[0]["abbreviation"] == "SAMB"


async def test_brokerage_distinguishes_account_and_receivable_payments(panel_client):
    client, container = panel_client
    async with container.session_factory() as session, session.begin():
        business = (await session.execute(select(Business))).scalar_one()
        business.vertical = BusinessVertical.BROKERAGE.value

    created = await client.post("/api/panel/v1/brokerage/members", json={
        "name": "Alex Example", "phone": "584140000099",
    })
    broker_id = created.json()["id"]

    account_payment = await client.post("/api/panel/v1/brokerage/account-payments", json={
        "amount": "20", "broker_id": broker_id, "description": "Devolución",
    })
    assert account_payment.status_code == 200, account_payment.text
    assert account_payment.json()["data"]["balance_raw"] == "20.00"

    removed_entry = await client.post("/api/panel/v1/brokerage/entries", json={
        "amount": "20", "broker_id": broker_id,
    })
    assert removed_entry.status_code == 404

    pending = await client.post("/api/panel/v1/brokerage/pending-purchases?confirmed=true", json={
        "exchange_rate": "800",
        "customer_name": "Juan", "amount": "50", "broker_id": broker_id, "force_new_customer": True, "customer_classification": "personal",
    })
    assert pending.status_code == 200, pending.text
    receivable_payment = await client.post("/api/panel/v1/brokerage/receivable-payments", json={
        "customer_name": "Juan", "amount": "20", "broker_id": broker_id,
    })
    assert receivable_payment.status_code == 200, receivable_payment.text
    assert receivable_payment.json()["data"]["customer_balance"]["display"] == "$30,00"

    removed = await client.post("/api/panel/v1/brokerage/movements", json={
        "direction": "out", "amount": "100", "broker_id": broker_id,
    })
    assert removed.status_code == 405

    summary = (await client.get("/api/panel/v1/brokerage/summary")).json()
    assert summary["totals"]["payments_to_accounts"] == "40.00"
    assert "account_payments" not in summary["totals"]
    assert "net" not in summary["totals"]
    assert "out" not in summary["totals"]


async def test_direct_withdrawal_api_permissions_dedup_and_idempotency(panel_client):
    from uuid import uuid4

    client, container = panel_client
    async with container.session_factory() as session, session.begin():
        business = (await session.scalars(select(Business))).one()
        business.vertical = BusinessVertical.BROKERAGE.value
    broker = (await client.post("/api/panel/v1/brokerage/members", json={
        "name": "Samu", "phone": "584140000098",
    })).json()
    path = "/api/panel/v1/brokerage/account-withdrawals"
    assert (await client.post(path, json={"amount": "500"})).status_code == 422
    body = {"amount": "500", "broker_id": broker["id"], "idempotency_key": str(uuid4())}
    first = await client.post(path, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["data"]["balance_raw"] == "-500.00"
    replay = await client.post(path, json=body)
    assert replay.status_code == 200 and replay.json()["affected"] == first.json()["affected"]
    changed_request = await client.post(path, json={**body, "amount": "1000"})
    assert changed_request.status_code == 409
    duplicate = await client.post(path, json={**body, "idempotency_key": str(uuid4())})
    assert duplicate.status_code == 409
    assert duplicate.json()["duplicate"]["id"] == first.json()["affected"][0]["entity_id"]
    rows = (await client.get("/api/panel/v1/brokerage/movements?type=account_withdrawal")).json()
    assert rows["total"] == 1 and rows["items"][0]["amount"] == "500.00"
    assert rows["items"][0]["balance_delta_display"] == "-$500,00"
    summary = (await client.get("/api/panel/v1/brokerage/summary")).json()
    assert summary["totals"]["withdrawals_from_accounts"] == "500.00"
    assert summary["totals"]["bought"] == summary["totals"]["sold"] == "0.00"

@pytest.mark.parametrize("endpoint,payload", [
    ("operations", {"operation_type": "buy", "operation_amount": "100"}),
    ("operations", {"operation_type": "sell", "operation_amount": "100"}),
    ("pending-purchases", {"customer_name": "Nuevo", "amount": "100", "customer_classification": "global"}),
    ("account-purchases", {"amount": "100"}),
])
async def test_brokerage_api_requires_bs_even_with_confirmation(panel_client, endpoint, payload):
    client, container = panel_client
    async with container.session_factory() as session, session.begin():
        business = (await session.execute(select(Business))).scalar_one()
        business.vertical = BusinessVertical.BROKERAGE.value
    response = await client.post(f"/api/panel/v1/brokerage/{endpoint}?confirmed=true", json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "missing_bolivars"
    assert "Dime la cantidad de bolívares" in response.json()["message"]
    movements = await client.get("/api/panel/v1/brokerage/movements")
    assert movements.json()["total"] == 0


async def test_brokerage_owner_has_implicit_house_account_and_filters_it(panel_client):
    client, container = panel_client
    async with container.session_factory() as session, session.begin():
        business = (await session.execute(select(Business))).scalar_one()
        business.vertical = BusinessVertical.BROKERAGE.value

    accounts = await client.get("/api/panel/v1/brokerage/accounts")
    assert accounts.status_code == 200
    assert accounts.json()[0] == {
        "key": "house", "kind": "house", "member_id": None, "name": "Casa",
        "abbreviation": None, "is_active": True, "manageable": False,
    }
    operation = await client.post("/api/panel/v1/brokerage/operations", json={
        "exchange_rate": "800",
        "operation_type": "buy", "operation_amount": "450", "customer_name": "Lissy", "customer_classification": "global",
    })
    assert operation.status_code == 200, operation.text
    assert operation.json()["data"]["broker"] == "Casa"
    assert operation.json()["data"]["commission"] == "$0,00"
    assert operation.json()["data"]["balance"] is None

    house = (await client.get(
        "/api/panel/v1/brokerage/movements?account_scope=house"
    )).json()
    assert house["total"] == 1
    assert house["items"][0]["account_kind"] == "house"
    assert house["items"][0]["member_id"] is None
    assert house["items"][0]["amount"] == "450.00"

    customer = await client.post("/api/panel/v1/customers", json={"name": "María", "phone": "04120000001", "customer_classification": "global"})
    assert customer.status_code == 200, customer.text
    customers = await client.get("/api/panel/v1/customers?q=maria")
    assert customers.status_code == 200, customers.text
    assert [row["name"] for row in customers.json()] == ["María"]


async def test_brokerage_multicurrency_api_roundtrip_and_filters(panel_client):
    client, container = panel_client
    async with container.session_factory() as session, session.begin():
        business = (await session.execute(select(Business))).scalar_one()
        business.vertical = BusinessVertical.BROKERAGE.value
    response = await client.post("/api/panel/v1/brokerage/operations", json={
        "operation_type": "buy", "operation_amount": "100", "operation_amount_bs": "80.000,00",
    })
    assert response.status_code == 200, response.text
    assert response.json()["data"]["exchange_rate"] == "800.00"
    mid = response.json()["affected"][0]["entity_id"]
    changed = await client.patch(f"/api/panel/v1/brokerage/movements/{mid}", json={
        "expected_version": 1, "operation_amount_bs": "82000",
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()["data"]["exchange_rate"] == "820.00"
    summary = (await client.get("/api/panel/v1/brokerage/summary")).json()
    assert summary["multicurrency"]["buy"]["amount_bs"] == "82000.00"
    assert (await client.get("/api/panel/v1/brokerage/movements?completeness=complete")).json()["total"] == 1
    assert (await client.get("/api/panel/v1/brokerage/movements?completeness=incomplete")).json()["total"] == 0


async def test_brokerage_attachment_upload_creation_gallery_and_detail(panel_client):
    client, container = panel_client
    async with container.session_factory() as session, session.begin():
        business = (await session.execute(select(Business))).scalar_one()
        business.vertical = BusinessVertical.BROKERAGE.value
    broker = (await client.post("/api/panel/v1/brokerage/members", json={
        "name": "Alex Example", "phone": "584140000099",
    })).json()
    image = io.BytesIO()
    Image.new("RGB", (640, 400), "white").save(image, "PNG")

    upload = await client.post(
        "/api/panel/v1/brokerage/attachments/uploads",
        files={"file": ("transferencia.png", image.getvalue(), "image/png")},
    )
    assert upload.status_code == 200, upload.text
    upload_id = upload.json()["attachment_upload_id"]
    created = await client.post("/api/panel/v1/brokerage/account-payments", json={
        "amount": "20", "broker_id": broker["id"], "attachment_upload_id": upload_id,
    })
    assert created.status_code == 200, created.text
    movement_id = created.json()["affected"][0]["entity_id"]

    movement = (await client.get(f"/api/panel/v1/brokerage/movements/{movement_id}")).json()
    assert movement["attachment"]["filename"] == "transferencia.png"
    assert movement["attachment"]["original_url"].startswith("data:image/png;base64,")
    gallery = (await client.get("/api/panel/v1/brokerage/attachments?type=account_payment")).json()
    assert gallery["total"] == 1
    assert gallery["items"][0]["movement_id"] == movement_id
    attachment_id = gallery["items"][0]["id"]
    detail = (await client.get(f"/api/panel/v1/brokerage/attachments/{attachment_id}")).json()
    assert detail["broker"] == "Alex Example"
    assert detail["original_url"].startswith("data:image/png;base64,")

    invalid = await client.post(
        "/api/panel/v1/brokerage/attachments/uploads",
        files={"file": ("falso.png", b"not-an-image", "image/png")},
    )
    assert invalid.status_code == 422


async def test_panel_customer_purchases_and_history(panel_client):
    client, _ = panel_client
    post = client.post
    await post("/api/panel/v1/sales", json={"items": [{"product": "Bujía", "quantity": "2", "unit_price": "3", "currency": "USD"}],
                                             "customer_name": "Moisés", "payment_status": "credit"})
    await post("/api/panel/v1/sales", json={"items": [{"product": "Cadena", "quantity": "1", "unit_price": "10", "currency": "USD"}],
                                             "customer_name": "Moisés"})
    await post("/api/panel/v1/receivables", json={"customer_name": "Moisés", "amount": "4", "currency": "USD", "description": "Dos cauchos"})
    await post("/api/panel/v1/payments", json={"customer_name": "Moisés", "amount": "5", "currency": "USD"})
    await post("/api/panel/v1/sales", json={"items": [{"product": "Aceite", "quantity": "1", "unit_price": "8", "currency": "USD"}],
                                             "customer_name": "Luis"})
    moises = next(c for c in (await client.get("/api/panel/v1/customers")).json() if c["name"] == "Moisés")

    detail = (await client.get(f"/api/panel/v1/customers/{moises['id']}")).json()
    # Dos ventas + un fiado suelto. El fiado de la venta a crédito no se cuenta otra vez; el abono no es una compra.
    assert detail["purchases"]["count"] == 3
    assert detail["purchases"]["total"]["usd_official"] == "20.0000"
    assert detail["purchases"]["last_date"] is not None

    history = (await client.get(f"/api/panel/v1/movements?customer_id={moises['id']}")).json()
    assert history["total"] == 4 and {m["customer"] for m in history["items"]} == {"Moisés"}
    assert sorted(m["kind"] for m in history["items"]) == ["payment", "receivable", "sale", "sale"]

async def test_panel_products_notes_rates_and_business(panel_client):
    client, _ = panel_client
    assert (await client.post("/api/panel/v1/products", json={"name": "Cadena 428", "price_amount": "12", "price_currency": "USD",
                                                               "unit_cost_amount": "7", "unit_cost_currency": "USD"})).status_code == 200
    dup = await client.post("/api/panel/v1/products", json={"name": "cadena 428"})
    assert dup.status_code == 409
    assert (await client.get("/api/panel/v1/products")).json()[0]["unit_cost_amount"] == "7.0000"
    assert (await client.post("/api/panel/v1/notes", json={"text": "Cerramos los domingos", "note_kind": "policy"})).status_code == 200
    assert (await client.get("/api/panel/v1/notes")).json()[0]["kind"] == "policy"
    typo = await client.post("/api/panel/v1/rates/custom", json={"rate": "9500", "label": "Binance"})
    assert typo.status_code == 409 and typo.json()["error"] == "needs_confirmation"
    assert (await client.post("/api/panel/v1/rates/custom?confirmed=true", json={"rate": "9500"})).status_code == 200
    assert (await client.post("/api/panel/v1/rates/custom", json={"rate": "950", "label": "Binance"})).status_code == 200
    assert (await client.get("/api/panel/v1/rates")).json()["current"]["custom"]["rate"] == "950,00"
    biz = (await client.get("/api/panel/v1/business")).json()
    upd = await client.patch("/api/panel/v1/business", json={"expected_version": biz["version"], "display_currency": "local"})
    assert upd.status_code == 200
    assert (await client.post("/api/panel/v1/cash/close", json={})).status_code == 200
    assert len((await client.get("/api/panel/v1/cash-sessions")).json()) == 1


async def test_panel_invalid_command_returns_422(panel_client):
    client, _ = panel_client
    resp = await client.post("/api/panel/v1/expenses", json={"description": "x", "amount": "-5"})
    assert resp.status_code == 422


async def test_system_admin_can_find_but_cannot_destroy_businesses(panel_client, session_factory):
    client, _ = panel_client
    # An ordinary OTP session remains unprivileged even for an allowlisted administrator.
    assert (await client.get("/api/panel/v1/system/businesses")).status_code == 403
    assert (await client.post(f"/api/panel/v1/system/businesses/{uuid4()}/clean")).status_code == 403
    assert (await client.delete(f"/api/panel/v1/system/businesses/{uuid4()}")).status_code == 403
    # Este login no solicita ni consume un código de WhatsApp.
    assert (await client.post("/api/panel/v1/auth/system-login", json={"phone": "584140000001", "password": "incorrecta"})).status_code == 401
    # Datos operativos que el reset debe borrar, sin alterar la configuración guardada en businesses.
    assert (await client.post("/api/panel/v1/products", json={"name": "Bujía", "price_amount": "3", "price_currency": "USD"})).status_code == 200
    assert (await client.post("/api/panel/v1/sales", json={"items": [{"product": "Bujía", "quantity": "1", "unit_price": "3", "currency": "USD"}],
                                                              "customer_name": "Moisés"})).status_code == 200
    assert (await client.post("/api/panel/v1/sales", json={"items": [{"product": "Bujía", "quantity": "1", "unit_price": "3", "currency": "USD"}],
                                                              "customer_name": "Moisés", "payment_status": "credit"})).status_code == 200
    assert (await client.post("/api/panel/v1/payments", json={"customer_name": "Moisés", "amount": "1", "currency": "USD"})).status_code == 200
    assert (await client.post("/api/panel/v1/expenses", json={"description": "Luz", "amount": "5", "currency": "USD"})).status_code == 200
    assert (await client.post("/api/panel/v1/cash/close", json={})).status_code == 200

    assert (await client.post("/api/panel/v1/auth/system-login", json={"phone": "584140000001", "password": "solo-para-test"})).status_code == 200
    businesses = await client.get("/api/panel/v1/system/businesses", params={"q": "414000"})
    assert businesses.status_code == 200
    row = businesses.json()[0]
    assert row["name"] == "El Rayo" and row["phones"] == ["584140000001"]

    reset = await client.post(f"/api/panel/v1/system/businesses/{row['id']}/clean")
    assert reset.status_code == 403
    deleted = await client.delete(f"/api/panel/v1/system/businesses/{row['id']}")
    assert deleted.status_code == 403
    async with session_factory() as database_session:
        assert await database_session.get(Business, row["id"]) is not None
        assert (await database_session.execute(select(func.count()).select_from(Product))).scalar_one() == 1


async def test_system_admin_can_log_in_without_a_business(redis, session_factory):
    """Un administrador puede recuperar una instalación nueva sin sembrar un dueño ficticio."""
    container, _ = build_test_container(
        Script(), system_admin_phones="584140000001", system_admin_password_hash=hash_password("solo-para-test"),
    )
    app = create_app(container.settings, container)
    app.state.container = container
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/panel/v1/auth/system-login", json={"phone": "+58 414 0000001", "password": "solo-para-test"})
        assert login.status_code == 200
        assert login.json() == {"business_id": None, "phone": "584140000001", "system_admin": True}
        assert (await client.get("/api/panel/v1/me")).json() == {
            "phone": "584140000001", "member": None, "capabilities": [], "business": None, "system_admin": True,
        }
        assert (await client.get("/api/panel/v1/system/businesses")).json() == []
    await container.close()


async def test_system_admin_bootstraps_an_allowed_brokerage_business(redis, session_factory):
    allowed = "584140000101,584140000102,584140000103"
    container, _ = build_test_container(
        Script(),
        allowed_phones=allowed,
        system_admin_phones="584140000001",
        system_admin_password_hash=hash_password("solo-para-test"),
    )
    app = create_app(container.settings, container)
    app.state.container = container
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/panel/v1/auth/system-login", json={
            "phone": "584140000001", "password": "solo-para-test",
        })
        assert login.status_code == 200
        phones = await client.get("/api/panel/v1/system/signup-phones")
        assert phones.json() == [
            {"phone": "584140000101", "available": True},
            {"phone": "584140000102", "available": True},
            {"phone": "584140000103", "available": True},
        ]
        rejected = await client.post("/api/panel/v1/system/businesses", json={
            "name": "Casa Example",
            "owner": {"name": "Papá de Alex", "phone": "584140009999"},
            "brokers": [],
        })
        assert rejected.status_code == 422
        missing_abbreviation = await client.post("/api/panel/v1/system/businesses", json={
            "name": "Casa Example",
            "owner": {"name": "Papá de Alex", "phone": "584140000101"},
            "brokers": [{"name": "Alex Example", "phone": "584140000102"}],
        })
        assert missing_abbreviation.status_code == 422

        created = await client.post("/api/panel/v1/system/businesses", json={
            "name": "Casa Example",
            "owner": {"name": "Papá de Alex", "phone": "584140000101", "abbreviation": ""},
            "brokers": [
                {"name": "Alex Example", "phone": "584140000102", "abbreviation": "SB"},
                {"name": "Samuel Ledezma", "phone": "584140000103", "abbreviation": "SL"},
            ],
        })
        assert created.status_code == 200, created.text
        payload = created.json()
        assert payload["vertical"] == "brokerage"
        assert payload["country_code"] == "VE"
        assert payload["timezone"] == "America/Caracas"
        assert payload["commission_rate"] == "0.01"
        assert [(member["name"], member["role"], member["abbreviation"]) for member in payload["members"]] == [
            ("Papá de Alex", "owner", None),
            ("Alex Example", "broker", "SB"),
            ("Samuel Ledezma", "broker", "SL"),
        ]
        assert all(message["welcome_message"].count("• ") == 3 for message in payload["members"])
        assert (await client.get("/api/panel/v1/system/signup-phones")).json() == [
            {"phone": "584140000101", "available": False},
            {"phone": "584140000102", "available": False},
            {"phone": "584140000103", "available": False},
        ]

    async with session_factory() as s:
        business = await s.get(Business, payload["id"])
        assert business is not None
        assert business.onboarding_state == OnboardingState.DONE.value and business.welcomed is True
        assert business.settings == {"broker_commission_rate": "0.01"}
        members = list((await s.execute(
            select(BusinessMember).where(BusinessMember.business_id == business.id).order_by(BusinessMember.created_at)
        )).scalars())
        assert [(member.phone, member.role) for member in members] == [
            ("584140000101", "owner"), ("584140000102", "broker"), ("584140000103", "broker"),
        ]
    assert (await container.identity.member_by_phone("584140000102")).member_role.value == "broker"
    await container.close()


async def test_system_routes_reject_a_regular_owner(redis, session_factory):
    container, console = build_test_container(Script())
    await container.rates.refresh()
    async with session_factory() as s, s.begin():
        business = Business(name="Sin permisos")
        s.add(business)
        await s.flush()
        s.add(BusinessMember(business_id=business.id, phone="584140000001"))
    app = create_app(container.settings, container)
    app.state.container = container
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        code = (await client.post("/api/panel/v1/auth/request-code", json={"phone": "584140000001"})).json()["dev_code"]
        assert console.sent
        assert (await client.post("/api/panel/v1/auth/verify", json={"phone": "584140000001", "code": code})).status_code == 200
        assert (await client.get("/api/panel/v1/system/businesses")).status_code == 403
    await container.close()


async def test_otp_cooldown_and_single_use_under_concurrency(panel_client):
    client, c = panel_client
    assert (await client.post("/api/panel/v1/auth/request-code", json={"phone": "+58 414 0000001"})).status_code == 429
    await c.redis.delete("otp:cooldown:584140000001")
    code = (await client.post("/api/panel/v1/auth/request-code", json={"phone": "584140000001"})).json()["dev_code"]
    responses = await asyncio.gather(*[
        client.post("/api/panel/v1/auth/verify", json={"phone": "584140000001", "code": code}) for _ in range(6)
    ])
    assert [r.status_code for r in responses].count(200) == 1
    assert not await c.redis.exists("otp:584140000001")


async def test_otp_attempt_limit_does_not_leave_immortal_keys(panel_client):
    client, c = panel_client
    await c.redis.delete("otp:cooldown:584140000001")
    code = (await client.post("/api/panel/v1/auth/request-code", json={"phone": "584140000001"})).json()["dev_code"]
    wrong = "000001" if code == "000000" else "000000"
    for _ in range(5):
        assert (await client.post("/api/panel/v1/auth/verify", json={"phone": "584140000001", "code": wrong})).status_code == 401
    assert (await client.post("/api/panel/v1/auth/verify", json={"phone": "584140000001", "code": code})).status_code == 429
    assert not await c.redis.exists("otp:584140000001")


async def test_panel_blocks_cross_site_writes(panel_client):
    client, _ = panel_client
    for path, body in (("/auth/logout", {}), ("/expenses", {"description": "Luz", "amount": "10", "currency": "USD"}),
                       ("/auth/request-code", {"phone": "584140000001"})):
        assert (await client.post(f"/api/panel/v1{path}", json=body, headers={"Origin": "https://evil.example"})).status_code == 403
    assert (await client.post("/api/panel/v1/expenses", json={"description": "Luz", "amount": "10", "currency": "USD"},
                              headers={"Origin": "http://localhost:5173"})).status_code == 200


@pytest.mark.parametrize("phone", ["abcdefgh", "00000000", "++58414abc", "１２３４５６７８９"])
async def test_invalid_phone_cannot_create_business(panel_client, phone):
    client, _ = panel_client
    assert (await client.post("/api/panel/v1/auth/request-code", json={"phone": phone})).status_code == 422


@pytest.mark.parametrize("path", ["/movements?limit=-1", "/movements?offset=-1", "/turns?limit=0"])
async def test_negative_pagination_is_validation_error(panel_client, path):
    client, _ = panel_client
    assert (await client.get(f"/api/panel/v1{path}")).status_code == 422


async def test_session_reads_current_identity_and_rejects_deleted_member(panel_client, session_factory):
    from sqlalchemy import delete
    from starlette.requests import Request

    from app.api.panel.auth import current_session

    client, c = panel_client
    identity = await c.identity.member_by_phone("584140000001")
    async with session_factory() as s, s.begin():
        business = await s.get(Business, identity.business.id)
        business.timezone = "America/Bogota"
    request = Request({"type": "http", "app": client._transport.app,
                       "headers": [(b"cookie", f"bolos_session={client.cookies['bolos_session']}".encode())]})
    assert (await current_session(request)).timezone == "America/Bogota"
    response = await client.get("/api/panel/v1/me")
    assert response.status_code == 200 and response.json()["business"]["timezone"] == "America/Bogota"
    async with session_factory() as s, s.begin():
        await s.execute(delete(BusinessMember).where(BusinessMember.id == identity.member_id))
    assert (await client.get("/api/panel/v1/me")).status_code == 401


async def test_tenant_isolation_on_reads_and_writes(panel_client, session_factory):
    from app.application.commands.expenses import RecordExpense
    from app.application.context import Actor, ExecContext
    from app.db.models import Customer, Product
    from app.domain.enums import ActorType

    client, c = panel_client
    async with session_factory() as s, s.begin():
        other = Business(name="Otro negocio")
        s.add(other)
        await s.flush()
        product = Product(business_id=other.id, name="Secreto", normalized_name="secreto")
        customer = Customer(business_id=other.id, name="Privado", normalized_name="privado")
        s.add_all([product, customer])
        await s.flush()
    expense = await c.bus.dispatch(RecordExpense(description="Secreto", amount="10", currency="USD"),
                                   ExecContext(business_id=other.id, actor=Actor(ActorType.PANEL)))
    eid = expense.affected[0].entity_id
    for path in (f"/customers/{customer.id}", f"/records/expense/{eid}"):
        assert (await client.get(f"/api/panel/v1{path}")).status_code == 404
    assert (await client.patch(f"/api/panel/v1/records/expense/{eid}", json={"changes": {"amount": "1"}})).status_code == 404
    assert (await client.delete(f"/api/panel/v1/records/expense/{eid}")).status_code == 404
    assert (await client.post("/api/panel/v1/products", json={"product_id": str(product.id), "name": "Hack"})).status_code == 404
    assert (await client.post("/api/panel/v1/customers", json={"customer_id": str(customer.id), "name": "Hack"})).status_code == 404
    assert (await client.delete(f"/api/panel/v1/customers/{customer.id}")).status_code == 404
    assert (await client.get("/api/panel/v1/products")).json() == []
    assert (await client.get("/api/panel/v1/customers")).json() == []
    assert (await client.get("/api/panel/v1/movements")).json()["total"] == 0


async def test_production_disables_dev_and_uses_secure_cookie_attributes(panel_client):
    client, c = panel_client
    c.settings = c.settings.model_copy(update={"app_env": "production"})
    app = create_app(c.settings, c)
    app.state.container = c
    await c.redis.delete("otp:cooldown:584140000001")
    from app.api.panel.auth import request_code
    code = await request_code(c, "584140000001")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as prod:
        for path in ("/docs", "/openapi.json", "/api/dev/media/x"):
            assert (await prod.get(path)).status_code == 404
        assert (await prod.post("/api/dev/chat", json={"phone": "584140000001", "text": "hola"})).status_code == 404
        response = await prod.post("/api/panel/v1/auth/verify", json={"phone": "584140000001", "code": code})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=none" in cookie
        # El chat del panel es productivo y usa el mismo pipeline/aislamiento que WhatsApp.
        assert (await prod.post("/api/panel/v1/chat", json={"text": "hola"})).status_code == 200
        logout = await prod.post("/api/panel/v1/auth/logout")
        assert "samesite=none" in logout.headers["set-cookie"].lower()
        assert (await prod.get("/api/panel/v1/me")).status_code == 401


async def test_health_detects_dead_embedded_worker(panel_client):
    client, c = panel_client
    c.settings.run_worker_in_api = True
    assert (await client.get("/health")).status_code == 503


async def test_webhook_retry_recovers_persisted_message_when_queue_failed(kapso_client, session_factory):
    client, c = kapso_client
    app = client._transport.app
    real_queue = app.state.arq

    class UnavailableQueue:
        async def enqueue_job(self, *args, **kwargs):
            raise ConnectionError("queue unavailable")

    app.state.arq = UnavailableQueue()
    with pytest.raises(ConnectionError):
        await post_signed(client, kapso_event("wamid.queue-failed", "hola"))
    app.state.arq = real_queue
    assert (await post_signed(client, kapso_event("wamid.queue-failed", "hola"))).json()["queued"] == 1
    assert (await post_signed(client, kapso_event("wamid.queue-failed", "hola"))).json()["queued"] == 0
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(InboundMessage))).scalar_one() == 1


async def test_concurrent_signup_creates_one_business(redis, session_factory):
    c, _ = build_test_container(Script(), allowed_phones="584149990002")
    try:
        identities = await asyncio.gather(*[c.identity.resolve("584149990002") for _ in range(4)])
        assert len({i.business.id for i in identities}) == 1
        async with session_factory() as s:
            assert (await s.execute(select(func.count()).select_from(Business))).scalar_one() == 1
    finally:
        await c.close()


async def test_panel_rates_exchanges_and_sale_price_vs_payment(panel_client):
    """Tasas por propósito, modo de cobro, cambios, y una venta que no cuadra devuelve las opciones (409 needs_choice)."""
    client, _ = panel_client
    api = "/api/panel/v1"
    assert (await client.post(f"{api}/rates", json={"rate": "750", "purpose": "funds"})).status_code == 200
    rates = (await client.get(f"{api}/rates")).json()
    assert rates["current"]["funds"]["rate"] == "750,00" and rates["funds"][0]["source"] == "manual"
    assert (await client.put(f"{api}/rates/charge-mode", json={"mode": "custom"})).status_code == 422  # sin tasa propia
    assert (await client.put(f"{api}/rates/charge-mode", json={"mode": "market"})).status_code == 200
    assert (await client.get(f"{api}/business")).json()["charge_rate_mode"] == "market"

    exchange = await client.post(f"{api}/exchanges", json={"out_amount": "100", "out_currency": "USD", "in_amount": "80000",
                                                          "in_currency": "VES", "in_method": "mobile_payment"})
    assert exchange.status_code == 200
    assert (await client.get(f"{api}/rates")).json()["current"]["funds"]["rate"] == "800,00"
    movements = (await client.get(f"{api}/movements", params={"kind": "exchange"})).json()
    assert movements["total"] == 2  # salida y entrada

    short = await client.post(f"{api}/sales", json={"items": [{"product": "espejo", "unit_price": "10", "currency": "USD"}],
                                                  "amount_paid": "3000", "paid_currency": "VES"})
    assert short.status_code == 409 and short.json()["error"] == "needs_choice"
    discount = next(o for o in short.json()["options"] if o["title"] == "Pagada con descuento")
    ok = await client.post(f"{api}/sales", json=discount["command_payload"])
    assert ok.status_code == 200
    sale_id = ok.json()["affected"][0]["entity_id"]
    detail = (await client.get(f"{api}/records/sale/{sale_id}")).json()
    assert detail["settlement"] == "discount" and detail["paid"]["currency"] == "VES" and detail["paid"]["amount"] == "3000.0000"
    assert detail["price"]["list_total"] == "10.0000" and detail["money"]["rate_applied_source"] == "market"

    expense = await client.post(f"{api}/expenses", json={"description": "almuerzo", "amount": "4000", "currency": "VES",
                                                        "category": "food"})
    detail = (await client.get(f"{api}/records/expense/{expense.json()['affected'][0]['entity_id']}")).json()
    assert detail["real_cost"]["usd"] == "5.0000" and detail["real_cost"]["rate_funds"] == "800.000000"


async def test_staff_owner_edits_profiles_without_changing_access_phone(panel_client):
    client, container = panel_client
    async with container.session_factory() as session, session.begin():
        business = (await session.execute(select(Business))).scalar_one()
        business.vertical = BusinessVertical.BROKERAGE.value
        other_business = Business(name="Otro negocio")
        session.add(other_business)
        await session.flush()
        outsider = BusinessMember(business_id=other_business.id, phone="584140000097", name="Otro dueño")
        session.add(outsider)
        await session.flush()
        outsider_id = str(outsider.id)

    broker = (await client.post("/api/panel/v1/brokerage/members", json={
        "name": "Alex Example", "phone": "584140000099",
    })).json()
    staff = (await client.get("/api/panel/v1/staff")).json()
    assert len(staff) == 2
    owner = next(row for row in staff if row["role"] == "owner")
    for member, name in [(owner, "Enrique Pérez"), (broker, "Alex Pérez")]:
        updated = await client.patch(f"/api/panel/v1/staff/{member['id']}", json={"name": name})
        assert updated.status_code == 200, updated.text
        assert updated.json()["name"] == name
        assert updated.json()["phone"] == member["phone"]
        assert (await client.patch(f"/api/panel/v1/staff/{member['id']}",
                                   json={"phone": "584140000098"})).status_code == 422
        assert (await client.patch(f"/api/panel/v1/staff/{member['id']}",
                                   json={"name": "  "})).status_code == 422

    assert (await client.patch(f"/api/panel/v1/brokerage/members/{broker['id']}",
                               json={"phone": "584140000098"})).status_code == 422
    assert (await client.patch(f"/api/panel/v1/staff/{outsider_id}",
                               json={"name": "Intruso"})).status_code == 404
    changed = await client.patch(f"/api/panel/v1/staff/{broker['id']}", json={"abbreviation": "SP"})
    assert changed.status_code == 200
    assert changed.json()["abbreviation"] == "SP"
    assert (await client.patch(f"/api/panel/v1/staff/{owner['id']}",
                               json={"abbreviation": "EP"})).status_code == 422

    code = (await client.post("/api/panel/v1/auth/request-code", json={"phone": broker["phone"]})).json()["dev_code"]
    assert (await client.post("/api/panel/v1/auth/verify",
                             json={"phone": broker["phone"], "code": code})).status_code == 200
    assert (await client.get("/api/panel/v1/staff")).status_code == 403
    assert (await client.patch(f"/api/panel/v1/staff/{broker['id']}",
                               json={"name": "No autorizado"})).status_code == 403
    assert (await client.patch(f"/api/panel/v1/brokerage/members/{broker['id']}/abbreviation",
                               json={"abbreviation": "SB"})).status_code == 200


async def test_brokerage_customer_search_and_selection_are_explicit(panel_client):
    from app.domain.text import normalize_name

    client, container = panel_client
    me = (await client.get("/api/panel/v1/me")).json()
    async with container.session_factory() as session, session.begin():
        business = await session.get(Business, me["business"]["id"])
        business.vertical = "brokerage"
        existing = Customer(business_id=business.id, name="QA V2 Global Pendiente",
                            normalized_name=normalize_name("QA V2 Global Pendiente"), classification="global")
        session.add(existing)
        await session.flush()
        existing_id = str(existing.id)
    matches = await client.get("/api/panel/v1/brokerage/customers?q=QA%20V2%20Global%20Samu")
    assert matches.status_code == 200
    assert matches.json()[0]["id"] == existing_id
    payload = {"customer_name": "QA V2 Global Samu", "amount": "1000", "operation_amount_bs": "800000"}
    ambiguous = await client.post("/api/panel/v1/brokerage/pending-purchases", json=payload)
    assert ambiguous.status_code == 409 and ambiguous.json()["error"] == "ambiguous"
    assert ambiguous.json()["candidate_ids"] == [existing_id]
    selected = await client.post("/api/panel/v1/brokerage/pending-purchases", json={**payload, "customer_id": existing_id})
    assert selected.status_code == 200, selected.text
    movement_id = selected.json()["affected"][0]["entity_id"]
    detail = (await client.get(f"/api/panel/v1/brokerage/movements/{movement_id}")).json()
    assert detail["customer_id"] == existing_id
    assert detail["customer_name"] == "QA V2 Global Pendiente"
    assert detail["operation_amount_bs"] == "800000.00"


async def test_brokerage_customer_search_allows_brokers_without_revealing_private_clients(panel_client):
    from app.domain.text import normalize_name

    client, container = panel_client
    me = (await client.get("/api/panel/v1/me")).json()
    async with container.session_factory() as session, session.begin():
        business = await session.get(Business, me["business"]["id"])
        business.vertical = "brokerage"
        broker = BusinessMember(business_id=business.id, phone="584140000022", name="Samuel", role="broker")
        other = BusinessMember(business_id=business.id, phone="584140000023", name="Alex", role="broker")
        session.add_all([broker, other])
        await session.flush()
        for name, scope, owner in [("Carlos Global", "global", None), ("Carlos Propio", "personal", broker.id),
                                   ("Carlos Ajeno", "personal", other.id)]:
            session.add(Customer(business_id=business.id, name=name, normalized_name=normalize_name(name),
                                 classification=scope, owner_broker_member_id=owner))
    code = (await client.post("/api/panel/v1/auth/request-code", json={"phone": "584140000022"})).json()["dev_code"]
    assert (await client.post("/api/panel/v1/auth/verify", json={"phone": "584140000022", "code": code})).status_code == 200
    results = await client.get("/api/panel/v1/brokerage/customers?q=Carlos")
    assert results.status_code == 200
    assert {row["name"] for row in results.json()} == {"Carlos Global", "Carlos Propio"}
    personal = next(row for row in results.json() if row["classification"] == "personal")
    assert personal["owner_broker_name"] == "Samuel"
    assert (await client.get("/api/panel/v1/customers?q=Carlos")).status_code == 403


@pytest.mark.parametrize("classification", ["global", "personal"])
async def test_standalone_brokerage_customer_creation_is_searchable_in_its_authorized_scope(panel_client, classification):
    client, container = panel_client
    me = (await client.get("/api/panel/v1/me")).json()
    async with container.session_factory() as session, session.begin():
        business = await session.get(Business, me["business"]["id"])
        business.vertical = "brokerage"
    broker = (await client.post("/api/panel/v1/brokerage/members", json={"name": "Samu", "phone": "584140000028"})).json()
    broker_id = broker["id"]
    created = await client.post("/api/panel/v1/customers", json={
        "name": "Zafiro Independiente", "customer_classification": classification,
        **({"owner_broker_member_id": broker_id} if classification == "personal" else {}),
    })
    assert created.status_code == 200, created.text
    results = (await client.get("/api/panel/v1/brokerage/customers?q=Zafiro")).json()
    assert len(results) == 1 and results[0]["classification"] == classification
    assert results[0]["owner_broker_member_id"] == (broker_id if classification == "personal" else None)


@pytest.mark.parametrize("house", [False, True])
@pytest.mark.parametrize("operation_type", ["buy", "sell"])
async def test_brokerage_definitive_api_persists_financial_effects(panel_client, house, operation_type):
    from decimal import Decimal
    from uuid import UUID

    from app.db.models import BrokerMovement

    client, container = panel_client
    async with container.session_factory() as session, session.begin():
        business = (await session.scalars(select(Business))).one()
        business.vertical = BusinessVertical.BROKERAGE.value
    broker_id = None
    if not house:
        member = await client.post("/api/panel/v1/brokerage/members", json={"name": "Alex", "phone": "584140000099"})
        assert member.status_code == 200, member.text
        broker_id = member.json()["id"]
    buy = operation_type == "buy"
    response = await client.post("/api/panel/v1/brokerage/operations", json={
        "operation_type": operation_type, "operation_amount": "987" if buy else "200",
        "operation_amount_bs": "789600" if buy else "160000", "broker_id": broker_id,
        **({"received_amount": "1000"} if buy else {}),
    })
    assert response.status_code == 200, response.text
    mid = response.json()["affected"][0]["entity_id"]
    delta = Decimal("0") if house else Decimal("22.87") if buy else Decimal("2")
    assert Decimal(response.json()["data"]["balance_raw"] or "0") == delta
    changed = await client.patch(f"/api/panel/v1/brokerage/movements/{mid}", json={
        "expected_version": 1, "exchange_rate": "850",
    })
    assert changed.status_code == 200, changed.text
    async with container.session_factory() as session:
        row = await session.get(BrokerMovement, UUID(mid))
        assert row.operation_amount == (987 if buy else 200)
        assert row.commission == (0 if house else Decimal("9.87") if buy else 2)
        assert row.excess == (13 if buy and not house else 0)
        assert row.received_amount == (1000 if buy else None)
        assert row.broker_member_id == (UUID(broker_id) if broker_id else None)
        assert row.balance_delta == delta and row.exchange_rate == 850
        assert row.operation_amount_bs == (838950 if buy else 170000)


async def test_customer_nickname_api_roundtrip_and_conflict(panel_client):
    client, _ = panel_client
    created = await client.post("/api/panel/v1/customers", json={"name": "Julia Example"})
    assert created.status_code == 200, created.text
    cid = created.json()["data"]["customer_id"]
    assert created.json()["data"]["nickname"] == "JUL"
    found = await client.get("/api/panel/v1/customers?q=jul")
    assert [c["id"] for c in found.json()] == [cid]
    only = await client.post("/api/panel/v1/customers", json={"nickname": "car"})
    assert only.status_code == 200 and only.json()["data"]["name"] is None
    only_id = only.json()["data"]["customer_id"]
    edited = await client.post("/api/panel/v1/customers", json={"customer_id": only_id, "name": "Carlos Rodríguez"})
    assert edited.status_code == 200 and edited.json()["data"]["nickname"] == "CAR"
    rejected = await client.post("/api/panel/v1/customers", json={"customer_id": only_id, "nickname": "júl"})
    assert rejected.status_code == 409 and "ocupado" in rejected.text
    detail = await client.get(f"/api/panel/v1/customers/{only_id}")
    assert detail.json()["name"] == "Carlos Rodríguez" and detail.json()["nickname"] == "CAR"


@pytest.mark.parametrize("classification", ["global", "personal"])
async def test_broker_updates_customer_profile_with_version_and_scope(panel_client, classification):
    client, container = panel_client
    me = (await client.get("/api/panel/v1/me")).json()
    async with container.session_factory() as session, session.begin():
        business = await session.get(Business, me["business"]["id"])
        business.vertical = "brokerage"
        broker = BusinessMember(business_id=business.id, phone="584140000022", name="Alex", role="broker")
        other = BusinessMember(business_id=business.id, phone="584140000023", name="Otro", role="broker")
        foreign_business = Business(name="Otro negocio", vertical="brokerage")
        session.add_all([broker, other, foreign_business])
        await session.flush()
        customer = Customer(business_id=business.id, normalized_name="", nickname="ALM", classification=classification,
                            owner_broker_member_id=broker.id if classification == "personal" else None)
        hidden = Customer(business_id=business.id, normalized_name="", nickname="SECRET", classification="personal", owner_broker_member_id=other.id)
        foreign = Customer(business_id=foreign_business.id, normalized_name="", nickname="ALM", classification="global")
        collision = Customer(business_id=business.id, normalized_name="", nickname="TAKEN", classification="global")
        session.add_all([customer, hidden, foreign, collision])
        await session.flush()
        cid, hidden_id, foreign_id = customer.id, hidden.id, foreign.id
    code = (await client.post("/api/panel/v1/auth/request-code", json={"phone": "584140000022"})).json()["dev_code"]
    assert (await client.post("/api/panel/v1/auth/verify", json={"phone": "584140000022", "code": code})).status_code == 200
    endpoint = f"/api/panel/v1/brokerage/customers/{cid}"
    body = {"expected_version": 1, "name": "Ángel Leon Macas", "nickname": "ALM"}
    detail = await client.get(f"/api/panel/v1/brokerage/customer-accounts/{cid}")
    assert detail.status_code == 200 and detail.json()["name"] is None and detail.json()["version"] == 1
    updated = await client.patch(endpoint, json=body)
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["name"] == "Ángel Leon Macas"
    assert updated.json()["data"]["customer_id"] == str(cid)
    assert (await client.patch(endpoint, json=body)).status_code == 409
    assert (await client.patch(endpoint, json={"name": "Cambio sin versión"})).status_code == 422
    assert (await client.patch(endpoint, json={"expected_version": 2})).status_code == 422
    assert (await client.patch(endpoint, json={"expected_version": 2, "nickname": "TAKEN"})).status_code == 409
    assert (await client.patch(endpoint, json={"expected_version": 2, "name": None, "nickname": None})).status_code == 422
    for forbidden_field, value in [("phone", "123"), ("notes", "nota"), ("customer_classification", "personal"),
                                   ("owner_broker_member_id", str(other.id))]:
        assert (await client.patch(endpoint, json={**body, forbidden_field: value})).status_code == 422
    for inaccessible_id in [hidden_id, foreign_id]:
        assert (await client.patch(f"/api/panel/v1/brokerage/customers/{inaccessible_id}", json=body)).status_code == 404
        assert (await client.get(f"/api/panel/v1/brokerage/customer-accounts/{inaccessible_id}")).status_code == 404
    detail = (await client.get(f"/api/panel/v1/brokerage/customer-accounts/{cid}")).json()
    assert detail["name"] == "Ángel Leon Macas" and detail["nickname"] == "ALM" and detail["version"] == 2
