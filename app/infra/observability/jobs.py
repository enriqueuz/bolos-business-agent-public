"""Worker error boundary, independently testable without process configuration."""
from __future__ import annotations

from functools import wraps

from arq import Retry

from app.logging import get_logger

log = get_logger(__name__)


def report_job_errors(func):
    @wraps(func)
    async def wrapped(ctx, *args, **kwargs):
        try:
            return await func(ctx, *args, **kwargs)
        except Retry:
            raise
        except Exception as exc:
            try:
                await ctx["container"].incidents.report(exc, context={"stage": "worker:" + func.__name__, "channel": "worker",
                                                                   "inbound_id": str(args[0]) if args else None})
            except Exception:
                log.error("worker.incident_reporting_failed")
            raise
    return wrapped
