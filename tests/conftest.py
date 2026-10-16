import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from jobboard_mcp import urlguard
from jobboard_mcp.config import Settings
from jobboard_mcp.http import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def public_dns(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline tests: every host resolves to one public address, and requests keep their
    hostname so respx routes match. Live tests use real DNS and pinning."""
    if request.node.get_closest_marker("live"):
        return

    async def resolve(_host: str, _port: int) -> list[str]:
        return ["93.184.215.14"]

    monkeypatch.setattr(urlguard, "resolve_host", resolve)
    monkeypatch.setattr(urlguard.GuardedTransport, "pin", False)


@pytest.fixture
async def http() -> AsyncIterator[HttpClient]:
    async def no_sleep(_seconds: float) -> None:
        return None

    client = HttpClient(Settings(), sleep=no_sleep)
    yield client
    await client.aclose()
