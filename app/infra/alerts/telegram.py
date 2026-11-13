"""Alertas al founder por Telegram. Sin configuración: solo loguea (y lo dice en el log, para no quedar ciego)."""

from __future__ import annotations

import httpx

from app.logging import get_logger

log = get_logger(__name__)


class TelegramAlerter:
    def __init__(self, bot_token: str | None, chat_id: str | None) -> None:
        self._token, self._chat = bot_token, chat_id

    async def alert(self, text: str) -> None:
        await self.send(text)

    async def send(self, text: str) -> bool:
        """Delivery acknowledgement for the incident worker's retries; alert stays compatible."""
        if not self._token or not self._chat:
            log.warning("alert.telegram_not_configured", text=text)
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(f"https://api.telegram.org/bot{self._token}/sendMessage",
                                             json={"chat_id": self._chat, "text": text[:3900]})
                response.raise_for_status()
                return bool(response.json().get("ok"))
        except httpx.HTTPError as exc:
            # La URL de Telegram contiene el token: no registrar el texto de la excepción.
            log.error("alert.telegram_failed", error=type(exc).__name__)
            return False
