"""Engine y sesiones async. Una sesión = una unidad de trabajo (commit o rollback completo)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def make_engine(database_url: str, *, echo: bool = False, lock_timeout_ms: int = 5000) -> AsyncEngine:
    # `lock_timeout` en TODAS las conexiones: ninguna escritura espera indefinidamente a otra que tiene el registro
    # (p. ej. dos abonos del mismo cliente). Al vencer, el CommandBus lo convierte en un aviso amable (409).
    return create_async_engine(database_url, echo=echo, pool_pre_ping=True, pool_size=10, max_overflow=10,
                               connect_args={"server_settings": {"lock_timeout": str(lock_timeout_ms)}})


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=True)


@asynccontextmanager
async def transaction(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Abre una sesión con transacción; commit al salir bien, rollback si hay excepción."""
    async with factory() as session, session.begin():
        yield session
