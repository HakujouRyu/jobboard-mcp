"""Live checks against the real job board APIs. Opt-in: ``uv run pytest -m live``.

They sweep the example watchlist rather than your own, so results don't depend on who runs them.
"""

import dataclasses
import os
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult

from jobboard_mcp.config import EXAMPLE_WATCHLIST, Settings
from jobboard_mcp.filters import SweepFilters
from jobboard_mcp.models import Ats, CompanyStatus, Remote
from jobboard_mcp.service import JobBoardService

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[1]
SERVER_ENV = {**os.environ, "WATCHLIST_PATH": str(EXAMPLE_WATCHLIST), "LOG_LEVEL": "WARNING"}
ACCEPTANCE = {
    "title_include": ["engineer", "sre", "product"],
    "remote": [Remote.REMOTE_US, Remote.REMOTE_GLOBAL],
    "min_pay": 100_000,
    "posted_within_days": 21,
}


@pytest.fixture
async def service() -> AsyncIterator[JobBoardService]:
    svc = JobBoardService(
        dataclasses.replace(Settings.from_env(), watchlist_path=EXAMPLE_WATCHLIST)
    )
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
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "jobboard_mcp.server"], cwd=str(ROOT), env=SERVER_ENV
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        assert {"sweep_boards", "get_posting", "add_company"} <= {t.name for t in tools.tools}
        result = await session.call_tool("sweep_boards", {"companies": ["Linear"]})
        assert isinstance(result, CallToolResult) and not result.is_error
        assert result.structured_content is not None
        assert result.structured_content["per_company"][0]["status"] == "ok"


# ------------------------------------------------------------------ M2


async def test_live_resolve_posit_old_name_token(service: JobBoardService) -> None:
    r = await service.resolve_board("Posit", "https://posit.co/careers/")
    assert (r.ats, r.token) == (Ats.GREENHOUSE, "rstudio")
    assert r.confidence is not None and r.confidence.value == "high"


async def test_live_find_company_posting_round_trip(service: JobBoardService) -> None:
    from jobboard_mcp.adapters import ADAPTERS

    listing = await ADAPTERS[Ats.ASHBY].list_jobs(service.http, "Linear", "linear")
    target = listing.jobs[0]
    found = await service.find_company_posting("Linear", target.title)
    assert found.board_found
    assert target.title in {c.job.title for c in found.candidates}
    assert found.candidates[0].score >= 90


async def test_live_is_posting_live(service: JobBoardService) -> None:
    from jobboard_mcp.adapters import ADAPTERS

    for ats, token in [(Ats.GREENHOUSE, "gitlab"), (Ats.ASHBY, "linear"), (Ats.LEVER, "outreach")]:
        job = (await ADAPTERS[ats].list_jobs(service.http, token, token)).jobs[0]
        assert (await service.is_posting_live(url=job.url)).live, job.url
    gone = await service.is_posting_live(ats=Ats.GREENHOUSE, token="gitlab", job_id="1")
    assert gone.live is False


async def test_live_stdio_readonly_hides_write_tools() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jobboard_mcp.server"],
        cwd=str(ROOT),
        env={**SERVER_ENV, "WATCHLIST_READONLY": "true"},
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
        assert "add_company" not in names and "update_company" not in names
        assert {"sweep_boards", "find_company_posting", "list_watchlist"} <= names
