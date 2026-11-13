"""Fixtures del pipeline con modelos con guion (compartidos por los tests de tests/agent)."""

import pytest
import pytest_asyncio

from tests.agent.harness import Script, build_test_container


@pytest.fixture
def script() -> Script:
    return Script()


@pytest_asyncio.fixture
async def env(script, redis, session_factory):
    container, console = build_test_container(script)
    await container.rates.refresh()
    yield container, console
    await container.close()
