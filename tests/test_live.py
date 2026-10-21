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
WORKDAY_TOKEN = "cox.wd1/cox_external_career_site_1"
ORACLE_TOKEN = "ehxr.fa.us2/cx_1"
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


def fast_boards(svc: JobBoardService) -> list[str]:
    """Watchlist companies on documented APIs. Workday boards are paced a request a second,
    so they get their own timing test."""
    from jobboard_mcp.adapters import ADAPTERS
    from jobboard_mcp.models import Source

    return [
        e.company
        for e in svc.store.load()
        if e.ats and not e.archived and ADAPTERS[e.ats].source is Source.DOCUMENTED_API
    ]


async def test_live_watchlist_mostly_ok(service: JobBoardService) -> None:
    result = await service.sweep(SweepFilters(today=service.today()))
    with_board = [c for c in result.per_company if c.status is not CompanyStatus.NO_BOARD]
    ok = [c for c in with_board if c.status is CompanyStatus.OK]
    assert with_board
    assert len(ok) / len(with_board) >= 0.8, [c.model_dump() for c in with_board]


async def test_live_acceptance_cold_and_warm(service: JobBoardService) -> None:
    boards = fast_boards(service)
    started = time.perf_counter()
    cold = await service.sweep(acceptance_filters(service), companies=boards)
    cold_s = time.perf_counter() - started
    started = time.perf_counter()
    warm = await service.sweep(acceptance_filters(service), companies=boards)
    warm_s = time.perf_counter() - started
    print(f"\ncold {cold_s:.2f}s, warm {warm_s:.2f}s, {len(cold.jobs)} jobs")
    assert cold_s < 20
    assert warm_s < 2
    assert warm.cache_hits == sum(c.status is CompanyStatus.OK for c in warm.per_company)
    assert [j.id for j in warm.jobs] == [j.id for j in cold.jobs]


@pytest.mark.parametrize(
    ("ats", "token", "term"),
    [(Ats.WORKDAY, WORKDAY_TOKEN, "engineer"), (Ats.ORACLE, ORACLE_TOKEN, "")],
)
async def test_live_careers_site_sweep(
    service: JobBoardService, ats: Ats, token: str, term: str
) -> None:
    """A cold tier 2 sweep: searches, then up to max_details detail calls, a second apart."""
    from jobboard_mcp.adapters import ADAPTERS

    (company,) = [e.company for e in service.store.load() if e.token == token]
    filters = SweepFilters(today=service.today(), title_include=[term] if term else [])
    started = time.perf_counter()
    result = await service.sweep(filters, companies=[company])
    elapsed = time.perf_counter() - started
    (board,) = result.per_company
    assert board.status is CompanyStatus.OK, board
    assert result.jobs, f"{company} has no matching jobs today"
    max_details = ADAPTERS[ats].max_details or 0
    assert elapsed < max_details + 15, f"cold {ats} sweep took {elapsed:.0f}s"
    assert all(j.source.value == "careers_site" for j in result.jobs)
    from_detail = {"startDate", "ExternalPostedStartDate"}
    assert any(j.date_source in from_detail for j in result.jobs), "no detail call landed"


async def test_live_greenhouse_board_not_truncated(service: JobBoardService) -> None:
    raw = (
        await service.http.get_json("https://boards-api.greenhouse.io/v1/boards/gitlab/jobs")
    ).data
    result = await service.sweep(SweepFilters(today=service.today()), companies=["GitLab"])
    (gitlab,) = result.per_company
    assert gitlab.total == raw["meta"]["total"] == len(raw["jobs"])


@pytest.mark.parametrize(
    ("ats", "token"),
    [
        (Ats.GREENHOUSE, "gitlab"),
        (Ats.ASHBY, "linear"),
        (Ats.LEVER, "outreach"),
        (Ats.WORKDAY, WORKDAY_TOKEN),
        (Ats.ORACLE, ORACLE_TOKEN),
        (Ats.WORKABLE, "huggingface"),
        (Ats.RIPPLING, "rippling"),
        (Ats.PERSONIO, "vivid"),
    ],
)
async def test_live_get_posting_each_ats(service: JobBoardService, ats: Ats, token: str) -> None:
    from jobboard_mcp.adapters import ADAPTERS

    listing = await ADAPTERS[ats].list_jobs(service.http, token, token, search=["engineer"])
    assert listing.jobs, f"{ats}/{token} has no jobs today"
    first = listing.jobs[0]
    by_url = await service.get_posting(url=first.url)
    assert by_url.id == first.id
    # Rippling's documented API has no descriptions.
    assert by_url.description_text or ats is Ats.RIPPLING


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
        assert isinstance(result, CallToolResult) and not result.is_error, result.content
        assert result.structured_content is not None
        linear = result.structured_content["per_company"][0]
        assert linear["status"] == "ok", linear


# ------------------------------------------------------------------ M2


async def test_live_resolve_posit_old_name_token(service: JobBoardService) -> None:
    r = await service.resolve_board("Posit", "https://posit.co/about/careers/")
    # The token is only reachable through the careers-page link, so show what the scan saw.
    assert (r.ats, r.token) == (Ats.GREENHOUSE, "rstudio"), r.model_dump_json(indent=1)
    assert r.confidence is not None and r.confidence.value == "high", r.model_dump_json(indent=1)


async def test_live_find_company_posting_round_trip(service: JobBoardService) -> None:
    from jobboard_mcp.adapters import ADAPTERS

    listing = await ADAPTERS[Ats.ASHBY].list_jobs(service.http, "Linear", "linear")
    target = listing.jobs[0]
    found = await service.find_company_posting("Linear", target.title)
    assert found.board_found, found.model_dump_json(indent=1)
    scores = [(c.score, c.job.title) for c in found.candidates]
    assert target.title in {title for _, title in scores}, scores
    assert scores[0][0] >= 90, scores


async def test_live_is_posting_live(service: JobBoardService) -> None:
    from jobboard_mcp.adapters import ADAPTERS

    boards = [
        (Ats.GREENHOUSE, "gitlab"),
        (Ats.ASHBY, "linear"),
        (Ats.LEVER, "outreach"),
        (Ats.WORKDAY, WORKDAY_TOKEN),
        (Ats.ORACLE, ORACLE_TOKEN),
        (Ats.WORKABLE, "huggingface"),
        (Ats.RIPPLING, "rippling"),
        (Ats.PERSONIO, "vivid"),
    ]
    for ats, token in boards:
        # Workday boards are searched; "engineer" keeps this to a page or two.
        search = ["engineer"] if ats is Ats.WORKDAY else []
        job = (await ADAPTERS[ats].list_jobs(service.http, token, token, search=search)).jobs[0]
        check = await service.is_posting_live(url=job.url)
        assert check.live, f"{job.url}: {check.reason}"
    gone = await service.is_posting_live(ats=Ats.GREENHOUSE, token="gitlab", job_id="1")
    assert gone.live is False, gone.reason


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
