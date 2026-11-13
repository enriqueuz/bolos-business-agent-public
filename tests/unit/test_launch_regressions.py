"""Regresiones de seguridad y transporte detectadas durante el QA de lanzamiento."""

import json

import httpx
import pytest
from pydantic import ValidationError

from app.agent.pipeline import WHATSAPP_MAX, _split
from app.application.commands.catalog import UpsertProduct
from app.application.commands.expenses import RecordExpense
from app.application.commands.sales import SaleItemInput
from app.channels.kapso.provider import KapsoProvider
from app.config import Settings
from app.domain.enums import RateKind
from app.infra.rates.dolarapi import DolarApiVeFetcher


def test_production_requires_attachment_url_style_with_the_other_bucket_credentials():
    values = {
        "database_url": "postgresql+asyncpg://test",
        "redis_url": "redis://test",
        "app_env": "production",
        "secret_key": "production-secret-value",
        "messaging_provider": "kapso",
        "kapso_api_key": "k",
        "kapso_phone_number_id": "1",
        "kapso_webhook_secret": "s",
        "gemini_api_key": "g",
        "attachments_s3_endpoint_url": "https://storage.example",
        "attachments_s3_access_key_id": "access",
        "attachments_s3_secret_access_key": "secret",
        "attachments_s3_bucket_name": "receipts",
        "attachments_s3_region": "iad",
    }

    with pytest.raises(ValidationError, match="ATTACHMENTS_S3_URL_STYLE"):
        Settings(**values)
    assert Settings(**values, attachments_s3_url_style="virtual-host").is_production


async def test_kapso_sends_one_image_with_link_and_caption():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.image"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = KapsoProvider(api_key="k", phone_number_id="1", webhook_secret="s", base_url="https://8.8.8.8",
                                 api_version="v24.0", http=http)
        result = await provider.send_image("584121234567", "https://signed.example/receipt", "Compra · hoy")

    assert result.ok
    assert seen == [{
        "messaging_product": "whatsapp",
        "to": "584121234567",
        "type": "image",
        "image": {"link": "https://signed.example/receipt", "caption": "Compra · hoy"},
    }]


def test_blank_names_and_descriptions_are_rejected():
    with pytest.raises(ValidationError):
        RecordExpense(description="   ", amount="10")
    with pytest.raises(ValidationError):
        SaleItemInput(product=" \t ", unit_price="2")
    with pytest.raises(ValidationError):
        UpsertProduct(name="X", price_amount="5")


def test_long_unbroken_reply_stays_within_whatsapp_limit():
    value = "a" * 9000
    parts = _split(value)
    assert all(len(p) <= WHATSAPP_MAX for p in parts)
    assert "".join(parts) == value


async def test_media_does_not_leak_api_key_on_redirect():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "8.8.8.8":
            return httpx.Response(302, headers={"location": "https://1.1.1.1/file"})
        return httpx.Response(200, content=b"audio", headers={"content-type": "audio/ogg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = KapsoProvider(api_key="private", phone_number_id="1", webhook_secret="s", base_url="https://8.8.8.8",
                                 api_version="v24.0", http=http)
        data, mime = await provider.download_media("https://8.8.8.8/file")
    assert data == b"audio" and mime == "audio/ogg"
    assert seen[0].headers["x-api-key"] == "private"
    assert "x-api-key" not in seen[1].headers


@pytest.mark.parametrize("url", ["http://8.8.8.8/x", "https://127.0.0.1/x", "https://169.254.169.254/latest", "https://[::1]/x"])
async def test_media_rejects_private_urls(url):
    def handler(request):
        pytest.fail("No se debe realizar una solicitud a la URL privada")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = KapsoProvider(api_key="k", phone_number_id="1", webhook_secret="s", base_url="https://8.8.8.8",
                                 api_version="v24.0", http=http)
        with pytest.raises(ValueError):
            await provider.download_media(url)


async def test_media_rejects_redirect_to_private_network():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://127.0.0.1/secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = KapsoProvider(api_key="k", phone_number_id="1", webhook_secret="s", base_url="https://8.8.8.8",
                                 api_version="v24.0", http=http)
        with pytest.raises(ValueError):
            await provider.download_media("https://1.1.1.1/file")
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["http", "NaN", "Infinity", "bad_date"])
async def test_one_failed_rate_endpoint_preserves_the_other(failure):
    def handler(request):
        if request.url.path == "/official":
            return httpx.Response(200, json={"promedio": 800})
        if failure == "http":
            return httpx.Response(503)
        return httpx.Response(200, json={"promedio": 900 if failure == "bad_date" else failure,
                                        "fechaActualizacion": "invalid" if failure == "bad_date" else None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        rates = await DolarApiVeFetcher("https://rates.test/official", "https://rates.test/market", http=http).fetch()
    assert len(rates) == 1
    assert rates[0].kind == RateKind.OFFICIAL and rates[0].rate == 800
