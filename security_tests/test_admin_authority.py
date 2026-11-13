"""Service-free checks for the administrative authentication boundary."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from app.api.panel.auth import (
    COOKIE,
    PanelSession,
    _serializer,
    _set_session_cookie,
    current_session,
)
from app.api.panel.router import require_system_admin, system_clean_business, system_delete_business


def container():
    return SimpleNamespace(settings=SimpleNamespace(
        secret_key="fictional-test-signing-key-only", system_admin_password_hash="fictional-version",
        is_production=False, is_system_admin=lambda phone: phone == "12025550100",
    ))


@pytest.mark.asyncio
async def test_otp_identity_cannot_gain_system_authority():
    ordinary = PanelSession(uuid4(), uuid4(), "12025550100", "UTC")
    with pytest.raises(HTTPException) as error:
        await require_system_admin(ordinary, container())
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_password_authenticated_authority_is_required_and_allowlisted():
    admin = PanelSession(None, None, "12025550100", "UTC", system_authenticated=True)
    assert await require_system_admin(admin, container()) == admin
    with pytest.raises(HTTPException):
        await require_system_admin(PanelSession(None, None, "12025550101", "UTC", system_authenticated=True), container())


@pytest.mark.asyncio
async def test_password_rotation_invalidates_administrative_cookie():
    c = container()
    response = Response()
    _set_session_cookie(c, response, PanelSession(None, None, "12025550100", "UTC"), system_authenticated=True)
    token = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    c.settings.system_admin_password_hash = "new-fictional-version"
    request = Request({"type": "http", "headers": [(b"cookie", f"{COOKIE}={token}".encode())],
                       "app": SimpleNamespace(state=SimpleNamespace(container=c))})
    with pytest.raises(HTTPException) as error:
        await current_session(request)
    assert error.value.status_code == 401


def test_ordinary_cookie_has_no_administrative_claim():
    c = container()
    response = Response()
    _set_session_cookie(c, response, PanelSession(uuid4(), uuid4(), "12025550100", "UTC"))
    token = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    assert "system_authenticated" not in _serializer(c).loads(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [system_clean_business, system_delete_business])
async def test_destructive_maintenance_is_blocked_before_database_access(operation):
    with pytest.raises(HTTPException) as error:
        await operation(uuid4(), PanelSession(None, None, "12025550100", "UTC"), None)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_legacy_system_cookie_has_no_system_authority():
    c = container()
    token = _serializer(c).dumps({"p": "12025550100", "admin_only": True})
    request = Request({"type": "http", "headers": [(b"cookie", f"{COOKIE}={token}".encode())],
                       "app": SimpleNamespace(state=SimpleNamespace(container=c))})
    session = await current_session(request)
    with pytest.raises(HTTPException) as error:
        await require_system_admin(session, c)
    assert error.value.status_code == 403
