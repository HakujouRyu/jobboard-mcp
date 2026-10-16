"""Live checks against the real job board APIs. Opt-in: ``uv run pytest -m live``."""

import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult

from jobboard_mcp.config import Settings
from jobboard_mcp.filters import SweepFilters
from jobboard_mcp.models import Ats, CompanyStatus, Remote
from jobboard_mcp.service import JobBoardService
from jobboard_mcp.watchlist import load_watchlist

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = {
    "title_include": ["engineer", "sre", "product"],
    "remote": [Remote.REMOTE_US],
    "min_pay": 100_000,
    "posted_within_days": 21,
}


@pytest.fixture
async def service() -> AsyncIterator[JobBoardService]:
    svc = JobBoardService(Settings.from_env())
    yield svc
    await svc.aclose()


def acceptance_filters(svc: JobBoardService) -> SweepFilters:
    return SweepFilters(today=svc.today(), **ACCEPTANCE)  # type: ignore[arg-type]


async def test_live_watchlist_mostly_ok(service: JobBoardService) -> None:
    result = await service.sweep(SweepFilters(today=service.today()))
    with_board = [c for c in result.per_company if c.status is not CompanyStatus.NO_BOARD]
    ok = [c for c in with_board if c.status is CompanyStatus.OK]
    assert with_board
    assert len(ok) / len(with_board) >= 0.8, [c.model_dump() for c in with_board]


async def test_live_acceptance_cold_and_warm(service: JobBoardService) -> None:
    started = time.perf_counter()
    cold = await service.sweep(acceptance_filters(service))
    cold_s = time.perf_counter() - started
    started = time.perf_counter()
    warm = await service.sweep(acceptance_filters(service))
    warm_s = time.perf_counter() - started
    print(f"\ncold {cold_s:.2f}s, warm {warm_s:.2f}s, {len(cold.jobs)} jobs")
    assert cold_s < 20
    assert warm_s < 2
    assert warm.cache_hits == sum(c.status is CompanyStatus.OK for c in warm.per_company)
    assert [j.id for j in warm.jobs] == [j.id for j in cold.jobs]


async def test_live_greenhouse_board_not_truncated(service: JobBoardService) -> None:
    raw = (
        await service.http.get_json("https://boards-api.greenhouse.io/v1/boards/gitlab/jobs")
    ).data
    result = await service.sweep(SweepFilters(today=service.today()), companies=["GitLab"])
    (gitlab,) = result.per_company
    assert gitlab.total == raw["meta"]["total"] == len(raw["jobs"])


@pytest.mark.parametrize(
    ("ats", "token"),
    [(Ats.GREENHOUSE, "gitlab"), (Ats.ASHBY, "linear"), (Ats.LEVER, "outreach")],
)
async def test_live_get_posting_each_ats(service: JobBoardService, ats: Ats, token: str) -> None:
    from jobboard_mcp.adapters import ADAPTERS

    listing = await ADAPTERS[ats].list_jobs(service.http, token, token)
    assert listing.jobs, f"{ats}/{token} has no jobs today"
    first = listing.jobs[0]
    by_url = await service.get_posting(url=first.url)
    assert by_url.id == first.id
    assert by_url.description_text


async def test_live_stdio_roundtrip() -> None:
    assert load_watchlist(Settings().watchlist_path)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "jobboard_mcp.server"], cwd=str(ROOT)
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        assert {t.name for t in tools.tools} == {"sweep_boards", "get_posting"}
        result = await session.call_tool("sweep_boards", {"companies": ["Linear"]})
        assert isinstance(result, CallToolResult) and not result.is_error
        assert result.structured_content is not None
        assert result.structured_content["per_company"][0]["status"] == "ok"
