"""The sweep's detail step: capped per board, and filters run again once details arrive."""

from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest

from jobboard_mcp.adapters import ADAPTERS
from jobboard_mcp.adapters.base import BoardListing, BoardProbe, LiveCheck, build_job
from jobboard_mcp.config import Settings
from jobboard_mcp.filters import SweepFilters
from jobboard_mcp.http import HttpClient
from jobboard_mcp.models import Ats, Job, Remote, Source, WatchlistEntry
from jobboard_mcp.service import JobBoardService

NOW = datetime(2026, 10, 15, 18, 0, tzinfo=UTC)
DETAIL_REMOTE = {"1": Remote.REMOTE_US, "2": Remote.ONSITE, "3": Remote.REMOTE_US}


class ListThenDetail:
    """An ATS whose list gives only titles, like Workday: remote status needs a detail call."""

    ats = Ats.GREENHOUSE
    source = Source.CAREERS_SITE
    guess_tokens = False
    detail_fields = frozenset({"remote", "posted_at"})
    max_details: int | None = 2

    def __init__(self) -> None:
        self.detail_calls: list[str] = []
        self.searched: Sequence[str] = ()
        self.today: date | None = None

    def _job(self, native_id: str, remote: Remote = Remote.UNKNOWN) -> Job:
        return build_job(
            ats=self.ats,
            token="t",
            native_id=native_id,
            company="Co",
            title=f"Engineer {native_id}",
            locations=[],
            remote=remote,
            source=self.source,
            url=f"https://co.test/{native_id}",
        )

    def list_url(self, token: str) -> str:
        return "https://co.test/jobs"

    async def list_jobs(self, http, company, token, *, refresh=False, search=(), today=None):
        self.searched, self.today = search, today
        return BoardListing([self._job(i) for i in ("1", "2", "3")], cache_hit=False)

    async def probe(self, http, token, *, refresh=False):
        return BoardProbe(3, set())

    async def get_job(self, http, company, token, job_id, *, refresh=False):
        self.detail_calls.append(job_id)
        return self._job(job_id, DETAIL_REMOTE[job_id])

    async def check_live(self, http, token, job_id, *, refresh=False):
        return LiveCheck(True, "", False)

    def needs_detail(self, job: Job) -> bool:
        return job.remote is Remote.UNKNOWN

    def parse_url(self, url: str):
        return None


@pytest.fixture
async def fake(monkeypatch: pytest.MonkeyPatch):
    adapter = ListThenDetail()
    monkeypatch.setitem(ADAPTERS, Ats.GREENHOUSE, adapter)  # type: ignore[arg-type]
    entries = [WatchlistEntry(company="Co", ats=Ats.GREENHOUSE, token="t")]
    svc = JobBoardService(
        Settings(), HttpClient(Settings()), watchlist=lambda: entries, now=lambda: NOW
    )
    yield adapter, svc
    await svc.aclose()


async def test_filters_run_again_after_details(fake) -> None:
    adapter, svc = fake
    result = await svc.sweep(
        SweepFilters(today=NOW.date(), title_include=["engineer"], remote=[Remote.REMOTE_US])
    )
    # Job 2 turned out on-site once its detail arrived, so it's dropped.
    assert sorted(j.native_id for j in result.jobs) == ["1", "3"]
    by_id = {j.native_id: j for j in result.jobs}
    assert by_id["1"].remote is Remote.REMOTE_US and by_id["1"].unverified_filters == []
    # Job 3 was past the cap: no detail call, kept as unknown and named as unverified.
    assert by_id["3"].remote is Remote.UNKNOWN and by_id["3"].unverified_filters == ["remote"]
    assert adapter.detail_calls == ["1", "2"]
    note = result.per_company[0].note
    assert note is not None and "first 2 of 3 matching jobs" in note


async def test_sweep_passes_title_terms_and_today(fake) -> None:
    adapter, svc = fake
    await svc.sweep(SweepFilters(today=NOW.date(), title_include=["engineer", "sre"]))
    assert list(adapter.searched) == ["engineer", "sre"]
    assert adapter.today == NOW.date()


async def test_no_note_under_the_cap(fake) -> None:
    adapter, svc = fake
    adapter.max_details = None
    result = await svc.sweep(SweepFilters(today=NOW.date()))
    assert result.per_company[0].note is None
    assert adapter.detail_calls == ["1", "2", "3"]
