"""Run explicitly in staging: uv run python -m app.infra.observability.smoke.

Creates a synthetic GitHub issue and Telegram alert; sends no WhatsApp message,
creates no financial record and needs no database connection.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import sentry_sdk

from app.config import get_settings
from app.infra.alerts.telegram import TelegramAlerter
from app.infra.observability.capture import SentryIncidentReporter, configure_sentry


async def main():
    settings = get_settings()
    if settings.incident_environment != "staging" or not settings.incidents_enabled:
        raise SystemExit("Requires SENTRY_ENVIRONMENT=staging and INCIDENTS_ENABLED=true. Refusing other environments.")
    configure_sentry(settings)
    reporter = SentryIncidentReporter(settings, SimpleNamespace(), TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id))
    try:
        raise RuntimeError("ERROR_SIMULADO_STAGING_OBSERVABILITY")
    except RuntimeError as exc:
        await reporter.report(exc, context={"stage": "smoke", "channel": "synthetic", "business_name": "Negocio sintético",
                                           "user_name": "Usuario de prueba", "role": "test", "message": "Orlando 100$ a tasa 800 (prueba sintética)"})
    await asyncio.to_thread(sentry_sdk.flush, timeout=3)
    print("Smoke submission attempted. Verify Sentry event, GitHub issue and Telegram; this is not delivery confirmation.")


if __name__ == "__main__":
    asyncio.run(main())
