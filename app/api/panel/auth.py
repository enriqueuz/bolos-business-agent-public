"""Auth del panel: OTP por WhatsApp para dueños y contraseña para la administración interna."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.application.context import Actor, ExecContext
from app.application.identity import normalize_phone
from app.container import Container
from app.domain.countries import DEFAULT_COUNTRY, country_profile, use_display_profile
from app.domain.enums import ActorType, BusinessVertical, MemberRole

COOKIE = "bolos_session"
SESSION_MAX_AGE_S = 30 * 24 * 3600
OTP_TTL_S = 300
OTP_MAX_ATTEMPTS = 5
PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000
PASSWORD_SEPARATOR = ":"  # `$` activa interpolación en Docker Compose al leer un archivo .env.

# Un único paso en Redis: dos verificaciones concurrentes no pueden consumir el mismo código.
_VERIFY_OTP = """
local hash = redis.call('HGET', KEYS[1], 'hash')
if not hash then return 0 end
local attempts = redis.call('HINCRBY', KEYS[1], 'attempts', 1)
if attempts > tonumber(ARGV[2]) then redis.call('DEL', KEYS[1]); return -1 end
if hash ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""


async def check_panel_origin(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    origin = request.headers.get("origin")
    allowed = {request.app.state.container.settings.panel_origin.rstrip("/"), str(request.base_url).rstrip("/")}
    if (origin and origin not in allowed) or (not origin and request.headers.get("sec-fetch-site") == "cross-site"):
        raise HTTPException(403, "Origen no permitido")


async def limit_login_requests(request: Request) -> None:
    container: Container = request.app.state.container
    # Usa la IP del servidor ASGI, nunca un X-Forwarded-For arbitrario enviado por el cliente.
    key = f"otp:ip:{request.client.host if request.client else 'unknown'}"
    async with container.redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, 600, nx=True)
        count, _ = await pipe.execute()
    if count > 30:
        raise HTTPException(429, "Demasiados intentos. Espera unos minutos.")


async def limit_admin_login_requests(request: Request) -> None:
    """Limita intentos de contraseña independientemente de los OTP."""
    container: Container = request.app.state.container
    key = f"admin-login:ip:{request.client.host if request.client else 'unknown'}"
    async with container.redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, 600, nx=True)
        count, _ = await pipe.execute()
    if count > 10:
        raise HTTPException(429, "Demasiados intentos. Espera unos minutos.")


@dataclass(frozen=True)
class PanelSession:
    # Una sesión del administrador del sistema no pertenece necesariamente a un
    # negocio. Mantener esa sesión separada de una identidad de negocio permite
    # recuperar/operar una base vacía sin crear un usuario ficticio.
    member_id: UUID | None
    business_id: UUID | None
    phone: str
    timezone: str
    country_code: str = DEFAULT_COUNTRY
    role: MemberRole = MemberRole.OWNER
    vertical: BusinessVertical = BusinessVertical.RETAIL

    system_authenticated: bool = False

    def ctx(self) -> ExecContext:
        if self.member_id is None or self.business_id is None:
            raise HTTPException(403, "La sesión de sistema no tiene un negocio activo.")
        return ExecContext(business_id=self.business_id, actor=Actor(ActorType.PANEL, self.member_id), timezone=self.timezone)

    @property
    def has_business(self) -> bool:
        return self.member_id is not None and self.business_id is not None


def _serializer(container: Container) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(container.settings.secret_key, salt="panel-session")


def _hash(code: str, secret: str) -> str:
    return hmac.new(secret.encode(), code.encode(), hashlib.sha256).hexdigest()


def hash_password(password: str, *, salt: bytes | None = None, iterations: int = PASSWORD_ITERATIONS) -> str:
    """Crea el valor que se guarda en SYSTEM_ADMIN_PASSWORD_HASH; se usa solo al configurar el despliegue."""
    if not password:
        raise ValueError("La contraseña no puede estar vacía.")
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    encoded_salt = base64.urlsafe_b64encode(salt).decode().rstrip("=")
    encoded_digest = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return PASSWORD_SEPARATOR.join((PASSWORD_SCHEME, str(iterations), encoded_salt, encoded_digest))


def verify_password(password: str, encoded: str) -> bool:
    """Verifica un hash PBKDF2 versionado sin exponer detalles de configuración al cliente."""
    try:
        # Acepta hashes previos con `$` para despliegues que no pasan por Docker Compose; los nuevos usan
        # `:` porque Compose interpolaría cualquier `$NOMBRE` dentro de un .env y dañaría el hash.
        separator = PASSWORD_SEPARATOR if PASSWORD_SEPARATOR in encoded else "$"
        scheme, raw_iterations, raw_salt, expected = encoded.split(separator, 3)
        if scheme != PASSWORD_SCHEME:
            return False
        iterations = int(raw_iterations)
        if not 100_000 <= iterations <= 2_000_000:
            return False
        padding = "=" * (-len(raw_salt) % 4)
        salt = base64.urlsafe_b64decode(raw_salt + padding)
        padding = "=" * (-len(expected) % 4)
        expected_digest = base64.urlsafe_b64decode(expected + padding)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        return hmac.compare_digest(actual, expected_digest)
    except (binascii.Error, ValueError, TypeError):
        return False


async def request_code(container: Container, phone: str) -> str | None:
    phone = normalize_phone(phone)
    if not await container.redis.set(f"otp:cooldown:{phone}", "1", nx=True, ex=60):
        raise HTTPException(429, "Espera un minuto antes de pedir otro código.")
    # `resolve` (no `member_by_phone`): si el teléfono no tiene negocio todavía, lo CREA cuando la
    # política de alta lo permite (mismo `SIGNUP_MODE`/`SIGNUP_ALLOWED_PHONES` que usa WhatsApp) — si
    # no, el comportamiento es idéntico al de antes (None, sin filtrar si el número existe o no).
    # Sin esto, una base vacía no tiene forma de entrar al panel ni siquiera para el primer negocio.
    identity = await container.identity.resolve(phone)
    if identity is None:
        return None
    code = f"{secrets.randbelow(1_000_000):06d}"
    key = f"otp:{identity.phone}"
    async with container.redis.pipeline(transaction=True) as pipe:
        pipe.hset(key, mapping={"hash": _hash(code, container.settings.secret_key), "attempts": 0})
        pipe.expire(key, OTP_TTL_S)
        await pipe.execute()
    result = await container.messaging.send_text(identity.phone, f"🔐 Tu código para entrar al panel de Bolos es *{code}*. Vence en 5 minutos.")
    if not result.ok:
        await container.redis.delete(key, f"otp:cooldown:{phone}")
        raise HTTPException(503, "No pude enviar el código. Intenta de nuevo en un momento.")
    return code


async def verify_code(container: Container, phone: str, code: str, response: Response) -> PanelSession:
    identity = await container.identity.member_by_phone(phone)
    if identity is None:
        raise HTTPException(401, "Código inválido")
    key = f"otp:{identity.phone}"
    verified = await container.redis.eval(_VERIFY_OTP, 1, key, _hash(code.strip(), container.settings.secret_key), OTP_MAX_ATTEMPTS)
    if verified == -1:
        raise HTTPException(429, "Demasiados intentos. Pide un código nuevo.")
    if verified != 1:
        raise HTTPException(401, "Código inválido o vencido. Pide uno nuevo.")
    session = _session_from_identity(identity)
    _set_session_cookie(container, response, session)
    return session


def _session_from_identity(identity) -> PanelSession:
    return PanelSession(identity.member_id, identity.business.id, identity.phone, identity.business.timezone,
                        identity.business.country_code, identity.member_role, identity.business.vertical)


def _set_session_cookie(container: Container, response: Response, session: PanelSession, *, system_authenticated: bool = False) -> None:
    data: dict[str, str | bool] = {"p": session.phone, "tz": session.timezone, "cc": session.country_code}
    if session.has_business:
        data.update({"m": str(session.member_id), "b": str(session.business_id)})
    else:
        # No basta una cookie firmada: current_session vuelve a comprobar que el
        # número siga autorizado por la configuración antes de aceptarla.
        data["admin_only"] = True
    if system_authenticated:
        data["system_authenticated"] = True
        data["password_version"] = hashlib.sha256(container.settings.system_admin_password_hash.encode()).hexdigest()
    token = _serializer(container).dumps(data)
    # El panel se despliega aparte del backend (Vercel ↔ Railway): en producción la cookie es
    # cross-origin, así que necesita SameSite=None + Secure (exige HTTPS, que ambos dan por defecto).
    # En desarrollo local sigue siendo same-origin (proxy de Vite), donde "lax" basta.
    response.set_cookie(COOKIE, token, max_age=SESSION_MAX_AGE_S, httponly=True,
                        samesite="none" if container.settings.is_production else "lax",
                        secure=container.settings.is_production, path="/")


async def verify_link_token(container: Container, token: str, response: Response) -> PanelSession:
    """Canjea el token del enlace de bienvenida por la sesión del panel, una sola vez.

    El token lo creó el agente al mandar el CTA del primer movimiento (`app/application/panel_links.py`) y solo
    vale 15 minutos. Se quema aquí: quien lo tenga después —una captura del chat, un recibo reenviado— no entra.
    Cualquier otro enlace del panel llega sin token y pasa por el código de siempre.
    """
    # `take` lo consume: el segundo toque, una captura del chat o un recibo reenviado no encuentran nada.
    data = await container.links.take(token)
    if data is None:
        raise HTTPException(401, "Ese enlace ya se usó o venció. Entra con tu número y te mando un código.")
    identity = await container.identity.member_by_phone(normalize_phone(data["p"]))
    if identity is None or str(identity.business.id) != data["b"]:
        raise HTTPException(401, "El enlace no es válido.")
    session = _session_from_identity(identity)
    _set_session_cookie(container, response, session)
    return session


async def verify_system_admin_password(container: Container, phone: str, password: str, response: Response) -> PanelSession:
    """Crea una sesión de sistema sin requerir que exista un negocio o miembro."""
    phone = normalize_phone(phone)
    password_ok = verify_password(password, container.settings.system_admin_password_hash)
    if not container.settings.is_system_admin(phone) or not password_ok:
        raise HTTPException(401, "Credenciales inválidas")
    # Si el administrador también es miembro, conserva su panel normal y el
    # menú de sistema. Si no existe (BD recién creada o negocio eliminado), se
    # emite la sesión administrativa independiente para poder recuperarla.
    identity = await container.identity.member_by_phone(phone)
    session = (
        _session_from_identity(identity)
        if identity is not None
        else PanelSession(member_id=None, business_id=None, phone=phone, timezone="America/Caracas")
    )
    _set_session_cookie(container, response, session, system_authenticated=True)
    return session


async def current_session(request: Request) -> PanelSession:
    container: Container = request.app.state.container
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "No autenticado")
    try:
        data = _serializer(container).loads(token, max_age=SESSION_MAX_AGE_S)
        system_authenticated = data.get("system_authenticated") is True
        if system_authenticated:
            # Administrative authority expires after eight hours and on password rotation.
            _serializer(container).loads(token, max_age=8 * 3600)
            expected_version = hashlib.sha256(container.settings.system_admin_password_hash.encode()).hexdigest()
            if not hmac.compare_digest(data.get("password_version", ""), expected_version):
                raise HTTPException(401, "Sesión inválida")
        if data.get("admin_only") is True:
            phone = data["p"]
            # Revocar el número en SYSTEM_ADMIN_PHONES invalida también las
            # sesiones de sistema que ya estaban emitidas.
            if not container.settings.is_system_admin(phone):
                raise HTTPException(401, "Sesión inválida")
            return PanelSession(member_id=None, business_id=None, phone=phone,
                                timezone=data.get("tz", "America/Caracas"),
                                country_code=data.get("cc", DEFAULT_COUNTRY), system_authenticated=system_authenticated)
        member_id, business_id = UUID(data["m"]), UUID(data["b"])
        identity = await container.identity.member_by_phone(data["p"])
        if identity is None or identity.member_id != member_id or identity.business.id != business_id:
            raise HTTPException(401, "Sesión inválida")
    except (BadSignature, SignatureExpired, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(401, "Sesión inválida") from exc
    session = PanelSession(member_id, business_id, identity.phone, identity.business.timezone,
                           identity.business.country_code, identity.member_role, identity.business.vertical,
                           system_authenticated=system_authenticated)
    use_display_profile(country_profile(session.country_code))
    request.state.incident_actor = {"user_id": str(identity.member_id), "user_name": identity.member_name,
                                    "business_id": str(identity.business.id), "business_name": identity.business.name,
                                    "role": identity.member_role.value}
    return session
