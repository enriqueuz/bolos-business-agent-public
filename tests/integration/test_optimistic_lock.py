"""Bloqueo optimista en la base de datos: dos escrituras que coinciden no se pisan en silencio."""

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.commands.catalog import UpsertProduct
from app.application.common import bump
from app.db.models import Product
from app.domain.errors import Stale


async def test_write_that_loses_the_race_is_rejected_not_overwritten(bus, panel_ctx, session_factory, monkeypatch):
    res = await bus.dispatch(UpsertProduct(name="Cadena 428", price_amount=Decimal(12), price_currency="USD"), panel_ctx)
    ref = res.affected[0]

    real_get, raced = AsyncSession.get, False

    async def racing_get(self, entity, ident, *a, **kw):
        nonlocal raced
        obj = await real_get(self, entity, ident, *a, **kw)
        if entity is Product and not raced:
            raced = True  # otro actor (el panel) confirma su edición justo después de que el agente leyó el registro
            async with session_factory() as other:
                theirs = await real_get(other, Product, ident)
                theirs.name = "Editado por el panel"
                bump(theirs)
                await other.commit()
        return obj

    monkeypatch.setattr(AsyncSession, "get", racing_get)
    edit = UpsertProduct(product_id=ref.entity_id, name="Editado por el agente", expected_version=ref.version)
    with pytest.raises(Stale):
        await bus.dispatch(edit, panel_ctx)
    monkeypatch.undo()

    async with session_factory() as s:
        p = await s.get(Product, ref.entity_id)
        assert p.name == "Editado por el panel" and p.version == 2  # la edición ganadora sigue intacta

    # reintentando con la versión vigente sí pasa
    ok = await bus.dispatch(UpsertProduct(product_id=ref.entity_id, name="Editado por el agente", expected_version=2), panel_ctx)
    assert ok.status == "ok"
    async with session_factory() as s:
        assert (await s.get(Product, ref.entity_id)).version == 3
