"""Configuración tipada. Los secretos no tienen defaults: si faltan en producción, la app no arranca."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class KapsoNumberSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    webhook_secret: str = Field(min_length=1, repr=False)
    api_key: str | None = Field(default=None, min_length=1, repr=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    database_url: str
    redis_url: str

    # Firma de las cookies del panel y de otros tokens. Obligatorio en producción.
    secret_key: str = Field(default="dev-only-secret-change-me", min_length=16)
    public_base_url: str = "http://localhost:8000"
    panel_origin: str = "http://localhost:5173"

    # ---- LLM: rol → "proveedor:modelo" (Pydantic AI). Cambiar de modelo = cambiar esto.
    llm_role_router: str = "google:gemini-3.1-flash-lite"
    llm_role_fast: str = "google:gemini-3.1-flash-lite"
    llm_role_agent: str = "google:gemini-3.8-flash"
    llm_role_onboarding: str = "google:gemini-3.1-flash-lite"
    llm_role_transcribe: str = "google:gemini-3.1-flash-lite"
    llm_role_vision: str = "google:gemini-3.1-flash-lite"
    gemini_api_key: str | None = None
    # Precios manuales (USD por millón de tokens) para modelos que genai-prices aún no conoce.
    # Formato JSON: {"gemini-3.8-flash": {"input": 0.5, "output": 3.0}}
    llm_price_overrides: str = "{}"
    # Cuánto razona el modelo del CARRIL AGENTE, en vocabulario neutro: off | low | medium | high.
    # `app/llm/thinking.py` lo traduce al dialecto de cada proveedor, así que este valor no cambia al cambiar de
    # modelo. Los demás roles no razonan (router y onboarding NO PUEDEN: fuerzan tool_choice y DeepSeek da 400).
    # "low" medido con evals en Gemini (15-09-2026): mismo acierto que el default, latencia máx. 44 s → 14 s.
    llm_agent_thinking: str | None = "low"
    # Igual para el CARRIL RÁPIDO, pero apagado por defecto: vacío = el piso del proveedor, sin tocar nada.
    # El router queda fuera a propósito (fuerza tool_choice y no puede razonar en DeepSeek).
    llm_fast_thinking: str | None = None
    # Tope por llamada al modelo: una respuesta lenta del proveedor (se vio una de 137 s) no deja al dueño esperando.
    llm_request_timeout_s: float = 45.0
    agent_request_limit: int = 8
    fast_request_limit: int = 4

    # ---- Mensajería
    messaging_provider: Literal["kapso", "console"] = "console"
    kapso_base_url: str = "https://api.kapso.ai/meta/whatsapp"
    kapso_api_version: str = "v24.0"
    kapso_platform_base_url: str = "https://api.kapso.ai/platform/v1"  # contactos (teléfono ↔ BSUID)
    kapso_api_key: str | None = None
    kapso_phone_number_id: str | None = None
    kapso_webhook_secret: str | None = None
    kapso_additional_numbers: dict[str, KapsoNumberSettings] = Field(default_factory=dict, repr=False)

    # ---- Acceso: quién puede crear un negocio escribiendo al número
    signup_mode: Literal["open", "allowlist", "closed"] = "allowlist"
    signup_allowed_phones: str = ""  # CSV de números E.164 sin '+', ej: 584141234567
    # Acceso al panel de operación de Bolos. No concede acceso al producto: solo habilita las rutas /system
    # para una sesión de panel cuyo teléfono ya exista. Vacío = nadie es administrador del sistema.
    system_admin_phones: str = ""  # CSV de números E.164 sin '+', ej: 584141234567
    # Formato pbkdf2_sha256:iteraciones:salt-base64:hash-base64. Nunca guardamos la contraseña en claro.
    system_admin_password_hash: str = ""

    # ---- País por defecto para números cuyo prefijo no coincide con ningún perfil (app/domain/countries.py)
    default_country: str = "VE"

    # ---- Despliegue de un solo servicio (piloto de bajo tráfico): el worker de arq corre DENTRO
    # del proceso de la API en vez de como contenedor aparte. Ver app/inprocess_worker.py.
    # Al activarlo, fija UVICORN_WORKERS=1 (Dockerfile): dos procesos uvicorn duplicarían el worker.
    run_worker_in_api: bool = False

    # ---- Alertas
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # ---- Incidents (disabled until credentials are configured)
    incidents_enabled: bool = False
    sentry_dsn: str | None = Field(default=None, repr=False)
    sentry_org: str = ""
    sentry_project: str = ""
    sentry_auth_token: str | None = Field(default=None, repr=False)
    sentry_base_url: str = "https://sentry.io"
    sentry_environment: str | None = None
    sentry_release: str | None = None
    github_token: str | None = Field(default=None, repr=False)
    github_repository: str = "example/disabled"
    deepseek_api_key: str | None = Field(default=None, repr=False)
    incident_analysis_model: str = "deepseek-flash"
    incident_analysis_daily_limit: int = Field(default=10, ge=0, le=100)

    @property
    def incident_environment(self) -> str:
        import os
        return self.sentry_environment or os.getenv("RAILWAY_ENVIRONMENT_NAME") or self.app_env

    # ---- Retención
    llm_payload_retention_days: int = 90

    # ---- Comprobantes privados (bucket S3-compatible dedicado)
    attachments_s3_endpoint_url: str | None = None
    attachments_s3_access_key_id: str | None = None
    attachments_s3_secret_access_key: str | None = None
    attachments_s3_bucket_name: str | None = None
    attachments_s3_region: str | None = None
    attachments_s3_url_style: Literal["path", "virtual", "virtual-host"] | None = None
    attachment_signed_url_ttl_s: int = Field(default=900, ge=60, le=3600)

    @property
    def allowed_phones(self) -> set[str]:
        return {p.strip().lstrip("+") for p in self.signup_allowed_phones.split(",") if p.strip()}

    @property
    def system_admin_phone_set(self) -> set[str]:
        return {p.strip().lstrip("+") for p in self.system_admin_phones.split(",") if p.strip()}

    def is_system_admin(self, phone: str) -> bool:
        return phone.lstrip("+") in self.system_admin_phone_set

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        import re
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.github_repository):
            raise ValueError("GITHUB_REPOSITORY must be owner/repository")
        if self.sentry_base_url.rstrip("/") not in {"https://sentry.io", "https://us.sentry.io", "https://de.sentry.io"}:
            raise ValueError("SENTRY_BASE_URL must be an official Sentry API origin")
        self.sentry_base_url = self.sentry_base_url.rstrip("/")
        if self.kapso_additional_numbers:
            if not self.kapso_phone_number_id or not self.kapso_api_key or not self.kapso_webhook_secret:
                raise ValueError("Los números adicionales requieren configurar también el número Kapso principal")
            if any(not number_id.isascii() or not number_id.isdigit() for number_id in self.kapso_additional_numbers):
                raise ValueError("KAPSO_ADDITIONAL_NUMBERS debe usar Phone Number IDs numéricos como claves")
            if self.kapso_phone_number_id in self.kapso_additional_numbers:
                raise ValueError("El número principal no debe repetirse en KAPSO_ADDITIONAL_NUMBERS")
        if bool(self.system_admin_phone_set) != bool(self.system_admin_password_hash):
            raise ValueError("SYSTEM_ADMIN_PHONES y SYSTEM_ADMIN_PASSWORD_HASH deben configurarse juntos")
        if self.app_env == "production":
            missing = []
            if self.secret_key.startswith("dev-only"):
                missing.append("SECRET_KEY")
            if self.messaging_provider != "kapso":
                missing.append("MESSAGING_PROVIDER=kapso (console no envía mensajes)")
            else:
                for name in ("kapso_api_key", "kapso_phone_number_id", "kapso_webhook_secret"):
                    if not getattr(self, name):
                        missing.append(name.upper())
            if not self.gemini_api_key:
                missing.append("GEMINI_API_KEY")
            for name in (
                "attachments_s3_endpoint_url",
                "attachments_s3_access_key_id",
                "attachments_s3_secret_access_key",
                "attachments_s3_bucket_name",
                "attachments_s3_region",
                "attachments_s3_url_style",
            ):
                if not getattr(self, name):
                    missing.append(name.upper())
            if missing:
                raise ValueError(f"Faltan variables obligatorias en producción: {', '.join(missing)}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
