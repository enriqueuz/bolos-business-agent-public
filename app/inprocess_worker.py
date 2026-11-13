"""Worker de arq embebido en el proceso de la API (modo RUN_WORKER_IN_API=true).

Solo para deploys de un único servicio facturable (piloto de bajo tráfico en Railway). Reutiliza el
`Container` ya construido por `app.main` (mismo pool de Postgres, mismo `RateService`, etc.) — NO
construye uno segundo, a diferencia del proceso standalone (`app.worker.startup`).

`app.worker` sigue siendo la única fuente de qué trabajo existe (`JOB_FUNCTIONS`, `CRON_JOBS`): este
módulo solo decide DÓNDE corre ese trabajo. Escalar a un worker separado más adelante es apagar
`RUN_WORKER_IN_API` y desplegar `app.worker.WorkerSettings` como proceso aparte — cero cambios aquí.

`handle_signals=False` es intencional: en este proceso, uvicorn/el lifespan de FastAPI son dueños del
ciclo de vida (SIGTERM, apagado ordenado). Si arq también instalara sus propios manejadores de señal,
competirían por los mismos signals dentro del mismo proceso.
"""

from __future__ import annotations

from arq.connections import RedisSettings
from arq.worker import Worker

from app.container import Container
from app.logging import get_logger
from app.worker import CRON_JOBS, JOB_FUNCTIONS

log = get_logger(__name__)


def build_inprocess_worker(container: Container) -> Worker:
    async def on_startup(ctx: dict) -> None:
        ctx["container"] = container
        await container.rates.refresh()
        log.info("inprocess_worker.started")

    async def on_shutdown(ctx: dict) -> None:
        # El Container lo cierra el lifespan de la API (es su dueño); aquí no hay nada propio que cerrar.
        log.info("inprocess_worker.stopped")

    return Worker(
        functions=JOB_FUNCTIONS,
        cron_jobs=CRON_JOBS,
        # Pool de Redis propio (no el `app.state.arq` de encolar): así `worker.close()` no le cierra
        # la conexión al resto de la API al apagarse.
        redis_settings=RedisSettings.from_dsn(container.settings.redis_url),
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        handle_signals=False,
        max_tries=150,
        job_timeout=240,
        keep_result=3600,
    )
