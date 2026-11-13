"""Gestión del negocio y equipo por el administrador, sin suplantar al dueño."""

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.api.panel.auth import hash_password
from app.application.commands.brokerage import RecordBrokerAccountPurchase
from app.application.commands.system_businesses import SystemRenameBusiness
from app.application.context import Actor, ExecContext
from app.db.models import AuditLog, BrokerMovement, Business, BusinessMember
from app.domain.enums import ActorType
from app.domain.errors import ValidationFailed
from app.main import create_app
from tests.agent.harness import Script, build_test_container

BASE = "/api/panel/v1"
PHONES = [f"58414000010{i}" for i in range(1, 6)]


@pytest_asyncio.fixture
async def system_house(redis, session_factory):
    container, _ = build_test_container(
        Script(), allowed_phones=",".join(PHONES), system_admin_phones="584140000001",
        system_admin_password_hash=hash_password("solo-para-test"),
    )
    app = create_app(container.settings, container)
    app.state.container = container
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(f"{BASE}/auth/system-login", json={
            "phone": "584140000001", "password": "solo-para-test",
        })
        assert login.status_code == 200
        response = await client.post(f"{BASE}/system/businesses", json={
            "name": "Casa inicial", "owner": {"name": "Dueño inicial", "phone": PHONES[0]},
            "brokers": [{"name": "Ana", "phone": PHONES[1], "abbreviation": "AN"}],
        })
        assert response.status_code == 200, response.text
        yield client, container, response.json()
    await container.close()


async def test_system_edits_profiles_and_adds_owner_to_same_house(system_house, session_factory):
    client, container, house = system_house
    path = f"{BASE}/system/businesses/{house['id']}"
    assert (await client.patch(path, json={"name": " Casa nueva "})).status_code == 200
    for member in house["members"]:
        body = {"name": member["name"] + " editado"}
        if member["role"] == "broker":
            body["abbreviation"] = "ae"
        edited = await client.patch(f"{path}/members/{member['id']}", json=body)
        assert edited.status_code == 200, edited.text
        assert edited.json()["phone"] == member["phone"]
        assert edited.json()["role"] == member["role"]
    added = await client.post(f"{path}/members", json={"name": "Nuevo dueño", "phone": PHONES[2], "role": "owner"})
    assert added.status_code == 200, added.text
    assert added.json()["abbreviation"] is None
    assert added.json()["welcome_message"].count("• ") == 3
    detail = (await client.get(path)).json()
    assert detail["name"] == "Casa nueva"
    assert [m["role"] for m in detail["members"]].count("owner") == 2
    assert next(m for m in detail["members"] if m["role"] == "broker")["abbreviation"] == "AE"
    identity = await container.identity.resolve(PHONES[2])
    assert str(identity.business.id) == house["id"]
    assert identity.member_role.value == "owner"
    # El nuevo dueño entra por OTP al negocio preparado; no dispara un segundo onboarding.
    async with httpx.AsyncClient(transport=client._transport, base_url="http://test") as owner_client:
        code = (await owner_client.post(f"{BASE}/auth/request-code", json={"phone": PHONES[2]})).json()["dev_code"]
        verified = await owner_client.post(f"{BASE}/auth/verify", json={"phone": PHONES[2], "code": code})
        assert verified.status_code == 200
        me = (await owner_client.get(f"{BASE}/me")).json()
        assert me["business"]["id"] == house["id"] and me["member"]["role"] == "owner"
        assert me["system_admin"] is False
        assert (await owner_client.get(f"{BASE}/staff")).status_code == 200
        assert (await owner_client.get(path)).status_code == 403
        assert (await owner_client.patch(path, json={"name": "No"})).status_code == 403
        assert (await owner_client.post(f"{path}/members", json={"name": "No", "phone": PHONES[3], "role": "owner"})).status_code == 403
        assert (await owner_client.patch(f"{path}/members/{house['members'][0]['id']}", json={"name": "No"})).status_code == 403
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Business))).scalar_one() == 1
        audits = (await s.execute(select(AuditLog).where(AuditLog.command == "system_create_member"))).scalars().all()
        assert len(audits) == 1 and audits[0].actor_type == "system"
        assert audits[0].after["member"]["id"] == added.json()["id"]


async def test_system_adds_brokers_and_validates_phone_and_abbreviation(system_house):
    client, _, house = system_house
    path = f"{BASE}/system/businesses/{house['id']}/members"
    body = {"name": "Nuevo corredor", "phone": PHONES[2], "role": "broker", "abbreviation": "NC"}
    for changes, status in [
        ({"phone": "584149999999"}, 422), ({"phone": PHONES[0]}, 409),
        ({"abbreviation": None}, 422), ({"abbreviation": "an"}, 409),
        ({"role": "admin"}, 422), ({"name": "  "}, 422),
    ]:
        rejected = await client.post(path, json={**body, **changes})
        assert rejected.status_code == status, rejected.text
    added = await client.post(path, json=body)
    assert added.status_code == 200 and added.json()["role"] == "broker"
    phones = (await client.get(f"{BASE}/system/signup-phones")).json()
    assert {"phone": PHONES[2], "available": False} in phones
    assert (await client.post(path, json=body)).status_code == 409


@pytest.mark.parametrize("body", [
    {"role": "owner"}, {"phone": PHONES[3]}, {"business_id": str(uuid4())},
    {"name": " "}, {"name": None}, {"abbreviation": None},
])
async def test_system_rejects_identity_changes_and_empty_profiles(system_house, body):
    client, _, house = system_house
    path = f"{BASE}/system/businesses/{house['id']}"
    member = house["members"][1]
    assert (await client.patch(f"{path}/members/{member['id']}", json=body)).status_code == 422
    detail = (await client.get(path)).json()
    current = next(m for m in detail["members"] if m["id"] == member["id"])
    assert current["name"] == member["name"]
    assert current["role"] == "broker"


async def test_system_preserves_owner_access_and_rejects_foreign_members(system_house):
    client, _, house = system_house
    path = f"{BASE}/system/businesses/{house['id']}"
    owner = house["members"][0]
    for body in [{"is_active": False}, {"is_active": True}, {"abbreviation": "DU"}]:
        assert (await client.patch(f"{path}/members/{owner['id']}", json=body)).status_code == 422
    other = (await client.post(f"{BASE}/system/businesses", json={
        "name": "Otra casa", "owner": {"name": "Otro dueño", "phone": PHONES[3]},
    })).json()
    foreign = other["members"][0]
    assert (await client.patch(f"{path}/members/{foreign['id']}", json={"name": "Intruso"})).status_code == 404
    assert (await client.get(f"{BASE}/system/businesses/{uuid4()}")).status_code == 404
    assert (await client.patch(path, json={"name": " "})).status_code == 422


async def test_system_deactivation_confirms_and_keeps_ledger(system_house, session_factory):
    client, container, house = system_house
    broker = house["members"][1]
    path = f"{BASE}/system/businesses/{house['id']}/members/{broker['id']}"
    ctx = ExecContext(business_id=UUID(house["id"]), actor=Actor(ActorType.PANEL, UUID(broker["id"])))
    purchase = await container.bus.dispatch(RecordBrokerAccountPurchase(amount="100", exchange_rate="800"), ctx, confirmed=True)
    preview = await client.patch(path, json={"name": "Ana editada", "is_active": False})
    assert preview.status_code == 409 and preview.json()["error"] == "needs_confirmation"
    async with session_factory() as s:
        member = await s.get(BusinessMember, UUID(broker["id"]))
        assert member.is_active and member.name == "Ana"
        assert not (await s.execute(select(AuditLog).where(AuditLog.command == "system_update_member"))).first()
    confirmed = await client.patch(path + "?confirmed=true", json={"name": "Ana editada", "is_active": False})
    assert confirmed.status_code == 200 and confirmed.json()["is_active"] is False
    assert await container.identity.member_by_phone(broker["phone"]) is None
    assert (await client.patch(path, json={"name": "Ana inactiva", "abbreviation": "AI"})).status_code == 200
    async with session_factory() as s:
        movement = await s.get(BrokerMovement, purchase.affected[0].entity_id)
        assert movement is not None and movement.deleted_at is None
    assert (await client.patch(path, json={"is_active": True})).status_code == 200
    assert await container.identity.member_by_phone(broker["phone"]) is not None


async def test_system_serializes_concurrent_member_creation(system_house, session_factory):
    client, _, house = system_house
    path = f"{BASE}/system/businesses/{house['id']}/members"
    body = {"name": "Nuevo dueño", "phone": PHONES[2], "role": "owner"}
    responses = await asyncio.gather(client.post(path, json=body), client.post(path, json=body))
    assert sorted(r.status_code for r in responses) == [200, 409]
    async with session_factory() as s:
        count = await s.scalar(select(func.count()).select_from(BusinessMember).where(BusinessMember.phone == PHONES[2]))
        assert count == 1


async def test_system_commands_reject_panel_actor(system_house):
    _, container, house = system_house
    ctx = ExecContext(business_id=UUID(house["id"]), actor=Actor(ActorType.PANEL, UUID(house["members"][0]["id"])))
    with pytest.raises(ValidationFailed, match="administración del sistema"):
        await container.bus.dispatch(SystemRenameBusiness(name="Sin permiso"), ctx)


async def test_system_and_whatsapp_share_phone_lock(system_house, session_factory):
    client, container, house = system_house
    path = f"{BASE}/system/businesses/{house['id']}/members"
    response, identity = await asyncio.gather(
        client.post(path, json={"name": "Nuevo dueño", "phone": PHONES[2], "role": "owner"}),
        container.identity.resolve(PHONES[2]),
    )
    assert response.status_code in {200, 409}
    async with session_factory() as s:
        members = (await s.execute(select(BusinessMember).where(BusinessMember.phone == PHONES[2]))).scalars().all()
        assert len(members) == 1 and members[0].id == identity.member_id
        if response.status_code == 200:
            assert str(identity.business.id) == house["id"]


async def test_system_edits_inactive_broker_and_guards_abbreviation_uniqueness(system_house):
    client, _, house = system_house
    path = f"{BASE}/system/businesses/{house['id']}/members"
    broker = house["members"][1]
    other = await client.post(path, json={"name": "Otro corredor", "phone": PHONES[2], "role": "broker", "abbreviation": "OC"})
    assert other.status_code == 200
    member_path = f"{path}/{broker['id']}"
    assert (await client.patch(member_path, json={"is_active": False})).status_code == 200
    response = await client.patch(member_path, json={"name": "No guardar", "abbreviation": "oc"})
    assert response.status_code == 409
    detail = (await client.get(f"{BASE}/system/businesses/{house['id']}")).json()
    current = next(m for m in detail["members"] if m["id"] == broker["id"])
    assert current["name"] == "Ana" and current["is_active"] is False
    assert (await client.patch(member_path, json={"name": "Ana editada", "abbreviation": "AE"})).status_code == 200
    assert (await client.patch(member_path, json={"is_active": True})).status_code == 200


async def test_system_editor_rejects_retail_businesses(system_house, session_factory):
    client, _, _ = system_house
    async with session_factory() as s, s.begin():
        retail = Business(name="Comercio")
        s.add(retail)
        await s.flush()
        business_id = str(retail.id)
    path = f"{BASE}/system/businesses/{business_id}"
    assert (await client.get(path)).status_code == 422
    assert (await client.patch(path, json={"name": "Cambio"})).status_code == 422
    assert (await client.post(f"{path}/members", json={"name": "Nuevo dueño", "phone": PHONES[2], "role": "owner"})).status_code == 422
