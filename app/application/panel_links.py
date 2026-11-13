"""Enlaces al panel: UNA gramática para todos los CTA que Bolos manda por WhatsApp.

Dos decisiones viven aquí, y las dos existen porque **un mensaje de WhatsApp no se puede editar**: los enlaces
que salgan hoy van a seguir en ese chat dentro de dos años y la gente va a seguir tocándolos.

1. **Forma canónica `/ir/<tipo>/<id>`, no la ruta real del panel.** El agente no sabe cómo se llaman las rutas
   del panel; emite el tipo de la entidad y el panel traduce a la vista que tenga en ese momento
   (`src/lib/deeplink.ts`). Renombrar `/app/fiados` mañana no rompe los recibos de hace meses. De paso, todos
   los enlaces pasan por un mismo sitio, que es donde se puede contar cuántos se abren.

2. **El enlace NO lleva credencial, salvo el primero del negocio.** Una URL con credencial es una llave: queda
   escrita en el historial del chat, sale en una captura y viaja si el dueño reenvía el recibo — y los dueños
   reenvían recibos. Así que los CTA de rutina van desnudos y quien no tenga sesión pasa por el código de
   siempre (el destino se preserva en `?redirect=`). La única excepción es el enlace de bienvenida, el primero
   que recibe un negocio recién configurado: ahí el toque tiene que salir redondo porque de él depende que el
   dueño entienda para qué sirve el panel, y es un momento en que está configurando solo, no compartiendo
   recibos.

Por qué el token es una clave opaca en Redis y no un token firmado que se explique solo: un token firmado con
los datos dentro medía 200 caracteres y llevaba el teléfono del dueño legible, en una URL que termina en el
historial del navegador y en los logs de acceso de quien sirva el panel. La clave opaca son 22 caracteres, no
dice nada de nadie, y el uso único sale gratis de consumirla con GETDEL. La dependencia de Redis no es nueva:
el uso único ya la exigía.
"""

from __future__ import annotations

import json
import secrets
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from app.domain.enums import EntityType

# Tipo de entidad → palabra del enlace. Es vocabulario PÚBLICO: cambiar una de estas palabras rompe los enlaces
# ya enviados, así que se agregan, no se renombran. El panel tiene la tabla espejo en `src/lib/deeplink.ts`.
SLUGS: dict[EntityType, str] = {
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
# Vista sin objeto concreto: cuando un turno tocó varios registros, el CTA lleva a la lista, no a uno de ellos.
MOVEMENTS_VIEW = "movimientos"

LINK_TOKEN_TTL_S = 15 * 60


class PanelLinks:
    """Construye los enlaces y administra el token de bienvenida. Uno por proceso, lo arma el contenedor."""

    def __init__(self, origin: str, redis: Redis) -> None:
        self._origin = origin.rstrip("/")
        self._r = redis

    def record(self, entity_type: EntityType, entity_id: UUID, *, token: str | None = None) -> str | None:
        """Enlace a un registro concreto. None si ese tipo no tiene dónde aterrizar en el panel."""
        slug = SLUGS.get(entity_type)
        return None if slug is None else self._url(f"{slug}/{entity_id}", token)

    def view(self, slug: str = MOVEMENTS_VIEW, *, token: str | None = None) -> str:
        return self._url(slug, token)

    def _url(self, path: str, token: str | None) -> str:
        return f"{self._origin}/ir/{path}" + (f"?t={token}" if token else "")

    async def mint(self, phone: str, business_id: UUID) -> str:
        """Token de bienvenida: entra al panel sin pedir código. Corto de vida y de un solo uso."""
        token = secrets.token_urlsafe(16)
        await self._r.set(f"panel-link:{token}", json.dumps({"p": phone, "b": str(business_id)}), ex=LINK_TOKEN_TTL_S)
        return token

    async def take(self, token: str) -> dict[str, Any] | None:
        """Consume el token: devuelve a quién pertenece la primera vez y None siempre después."""
        raw = await self._r.getdel(f"panel-link:{token}")
        if not raw:
            return None
        data = json.loads(raw)
        return data if data.get("p") and data.get("b") else None
