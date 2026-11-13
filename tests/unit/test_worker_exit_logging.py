"""Si el worker embebido muere, la causa queda en los logs (antes solo se veía un 503 en /health)."""

from __future__ import annotations

import asyncio

import pytest

import app.main as main_module


class _RecordingLog:
    def __init__(self) -> None:
        self.errors: list[tuple[str, dict]] = []

    def error(self, event: str, **kw) -> None:
        self.errors.append((event, kw))


async def _boom() -> None:
    raise RuntimeError('relation "businesses" does not exist')


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> _RecordingLog:
    rec = _RecordingLog()
    monkeypatch.setattr(main_module, "log", rec)  # structlog cachea el logger: no se puede capturar con capture_logs
    return rec


@pytest.mark.asyncio
async def test_worker_crash_is_logged_with_its_cause(recorded: _RecordingLog):
    task = asyncio.create_task(_boom())
    await asyncio.wait([task])
    main_module._log_worker_exit(task)
    assert len(recorded.errors) == 1
    event, kw = recorded.errors[0]
    assert event == "main.worker_died"
    assert "businesses" in kw["error"]
    assert isinstance(kw["exc_info"], RuntimeError)  # con la traza completa


@pytest.mark.asyncio
async def test_cancelled_worker_is_not_reported_as_a_failure(recorded: _RecordingLog):
    task = asyncio.create_task(asyncio.sleep(60))
    task.cancel()
    await asyncio.wait([task])
    main_module._log_worker_exit(task)
    assert recorded.errors == []
