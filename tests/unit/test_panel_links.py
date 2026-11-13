"""Los enlaces al panel: la forma de la URL y el token del enlace de bienvenida.

Lo que se protege aquí es que un mensaje de WhatsApp no se puede editar. Las palabras de `/ir/<tipo>/<id>` son
vocabulario público: renombrar una rompe todos los recibos que ya salieron, así que el test las escribe una por
una a mano. Si alguien las cambia, tiene que cambiarlas también aquí y ver por qué.
"""

from __future__ import annotations

from uuid import uuid4

from app.application.panel_links import SLUGS, PanelLinks
from app.domain.enums import EntityType

ORIGIN = "https://panel.example.invalid"

# Tipos que NO llevan enlace, y por qué. Un EntityType nuevo obliga a decidir a cuál de las dos listas entra.
WITHOUT_DESTINATION = {
    EntityType.BUSINESS,  # la configuración del negocio no es un registro que se vaya a "ver"
    EntityType.BROKER_ATTACHMENT,  # se abre siempre a través del movimiento autorizado que lo contiene
    EntityType.CUSTOMER_BALANCE_ADJUSTMENT,  # se presenta dentro de la cuenta, sin detalle comercial independiente
}


def links(redis=None) -> PanelLinks:
    return PanelLinks(ORIGIN, redis)


def test_every_entity_type_either_has_a_destination_or_is_deliberately_left_out():
    """El test que avisa: al agregar un EntityType hay que decidir si tiene a dónde llevar en el panel."""
    undecided = set(EntityType) - set(SLUGS) - WITHOUT_DESTINATION
    assert not undecided, f"decide si estos tipos llevan enlace al panel: {sorted(t.value for t in undecided)}"


def test_the_words_of_the_public_urls_are_the_ones_already_sent_by_whatsapp():
    assert SLUGS == {
        EntityType.BROKER_MOVEMENT: "corredores",
        EntityType.SALE: "venta",
        EntityType.EXPENSE: "gasto",
        EntityType.PURCHASE: "compra",
        EntityType.RECEIVABLE: "fiado",
        EntityType.PAYMENT: "abono",
        EntityType.EXCHANGE: "cambio",
        EntityType.CUSTOMER: "cliente",
        EntityType.PRODUCT: "producto",
        EntityType.CASH_SESSION: "cierre",
        EntityType.CUSTOM_RATE: "tasa",
        EntityType.NOTE: "nota",
    }


def test_a_record_link_carries_the_type_and_the_id_and_nothing_else():
    sale = uuid4()
    assert links().record(EntityType.SALE, sale) == f"{ORIGIN}/ir/venta/{sale}"


def test_a_type_without_destination_gets_no_link_instead_of_a_broken_one():
    assert links().record(EntityType.BUSINESS, uuid4()) is None


def test_the_routine_link_carries_no_credential():
    """La razón de ser de la decisión: un recibo reenviado no puede entregar la sesión del negocio."""
    assert "?" not in links().record(EntityType.EXPENSE, uuid4())
    assert "?" not in links().view()


async def test_the_welcome_link_carries_the_token_and_says_whose_it_is_exactly_once(redis):
    """El uso único es lo que hace tolerable que exista: el segundo intento no encuentra nada."""
    lk = links(redis)
    business, phone = uuid4(), "584140000001"
    token = await lk.mint(phone, business)
    assert lk.record(EntityType.SALE, uuid4(), token=token).endswith(f"?t={token}")
    assert await lk.take(token) == {"p": phone, "b": str(business)}
    assert await lk.take(token) is None


async def test_the_token_says_nothing_about_the_owner_and_stays_short(redis):
    """Va en una URL que termina en el historial del navegador y en los logs de quien sirva el panel."""
    token = await links(redis).mint("584140000001", uuid4())
    assert "584140000001" not in token and len(token) <= 32


async def test_a_token_nobody_minted_here_opens_nothing(redis):
    assert await links(redis).take("cualquier-cosa") is None
    assert await links(redis).take("") is None


def test_the_origin_is_used_as_given_even_with_a_trailing_slash():
    sale = uuid4()
    assert PanelLinks(f"{ORIGIN}/", None).record(EntityType.SALE, sale) == f"{ORIGIN}/ir/venta/{sale}"
