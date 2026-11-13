"""App FastAPI: webhook, API del panel, simulador de desarrollo y health."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import dev, webhooks
from app.api.panel import router as panel_router
from app.config import Settings, get_settings
from app.container import Container, build_container
from app.domain.errors import DomainError
from app.infra.observability.http import IncidentMiddleware
from app.logging import configure_logging, get_logger

log = get_logger(__name__)


def _log_worker_exit(task: asyncio.Task) -> None:
    """Una tarea de fondo que muere no escribe nada por sí sola (solo /health devolvería 503): déjalo en los logs."""
    if task.cancelled():
        return
    exc = task.exception()
    log.error("main.worker_died", error=repr(exc) if exc else "terminó sin error", exc_info=exc)


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json=settings.is_production)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container or build_container(settings)
        app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))

        # Modo de un solo servicio (ver app/inprocess_worker.py): la cola sigue existiendo en Redis
        # igual que siempre, solo que quien la consume vive en este mismo proceso en vez de en un
        # contenedor `worker` aparte. Fuera de este bloque, nada del pipeline sabe que esto pasó.
        worker_task: asyncio.Task | None = None
        arq_worker = None
        stopping = False  # el apagado normal también termina la tarea: eso no es un fallo
        if settings.run_worker_in_api:
            from app.inprocess_worker import build_inprocess_worker

            arq_worker = build_inprocess_worker(app.state.container)
            worker_task = asyncio.create_task(arq_worker.async_run())
            worker_task.add_done_callback(lambda t: None if stopping else _log_worker_exit(t))
            app.state.worker_task = worker_task
            log.info("main.worker_in_api_enabled")

        yield

        stopping = True
        if arq_worker is not None:
            await arq_worker.close()
            with suppress(asyncio.CancelledError):
                await worker_task
        await app.state.arq.aclose()
        await app.state.container.close()

    app = FastAPI(
        title="Bolos Negocios API", version="0.1.0", lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs", redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.add_middleware(IncidentMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=[settings.panel_origin], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        # La línea de acceso de uvicorn solo dice "409 Conflict"; aquí queda cuál error de dominio fue y por qué.
        log.warning("http.domain_error", method=request.method, path=request.url.path, status=exc.http_status,
                    error=exc.code, reason=exc.message, details=exc.details)
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.get("/health")
    async def health(request: Request) -> dict:
        if settings.run_worker_in_api and (not getattr(request.app.state, "worker_task", None) or request.app.state.worker_task.done()):
            raise HTTPException(503, "Worker no disponible")
        c: Container = request.app.state.container
        async with c.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await c.redis.ping()
        return {"status": "ok"}

    app.include_router(webhooks.router)
    app.include_router(panel_router.router)
    if not settings.is_production:
        app.include_router(dev.router)
    return app
