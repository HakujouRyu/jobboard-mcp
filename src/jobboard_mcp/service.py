"""Tool logic, kept apart from the MCP wiring so it can be tested directly.

A sweep runs in two phases: fetch every board in parallel and apply the cheap filters, then
fetch Greenhouse detail pages (pay transparency) only for the survivors that still lack pay.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from jobboard_mcp.adapters import ADAPTERS, parse_posting_url
from jobboard_mcp.adapters.base import BoardListing
from jobboard_mcp.config import Settings
from jobboard_mcp.filters import SweepFilters, flag, passes_pay, prefilter, sort_jobs
from jobboard_mcp.http import FetchError, HttpClient
from jobboard_mcp.models import (
    Ats,
    CompanyResult,
    CompanyStatus,
    Job,
    SweepResult,
    WatchlistEntry,
)
from jobboard_mcp.watchlist import load_watchlist

log = logging.getLogger(__name__)

_PAY_FIELDS = ("pay_min", "pay_max", "pay_currency", "pay_interval", "pay_raw", "pay_source")


@dataclass
class _Fetch:
    entry: WatchlistEntry
    listing: BoardListing | None = None
    error: Exception | None = None
    latency_ms: int = 0


def _status(exc: Exception) -> CompanyStatus:
    if isinstance(exc, FetchError):
        return CompanyStatus(exc.kind)
    return CompanyStatus.ERROR


class JobBoardService:
    def __init__(
        self,
        settings: Settings,
        http: HttpClient | None = None,
        *,
        watchlist: Callable[[], list[WatchlistEntry]] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.http = http or HttpClient(settings)
        self._watchlist = watchlist or (lambda: load_watchlist(settings.watchlist_path))
        self._now = now

    async def aclose(self) -> None:
        await self.http.aclose()

    def today(self) -> date:
        return self._now().date()

    # ------------------------------------------------------------------ sweep

    async def sweep(
        self,
        filters: SweepFilters,
        *,
        companies: Sequence[str] | None = None,
        include_description: bool = False,
        refresh: bool = False,
    ) -> SweepResult:
        entries = self._watchlist()
        results: dict[str, CompanyResult] = {}
        order = [e.company for e in entries]  # per_company follows this order
        if companies:
            by_name = {e.company.casefold(): e for e in entries}
            picked: list[WatchlistEntry] = []
            order = []
            for name in companies:
                entry = by_name.get(name.strip().casefold())
                if entry is None:
                    results[name] = CompanyResult(
                        company=name, status=CompanyStatus.ERROR, error="not in watchlist"
                    )
                    order.append(name)
                elif entry not in picked:
                    picked.append(entry)
                    order.append(entry.company)
            entries = picked

        fetches = await asyncio.gather(*(self._fetch_board(e, refresh) for e in entries))
        cache_hits = 0
        candidates: list[tuple[str, Job]] = []
        for f in fetches:
            e = f.entry
            if e.ats is None or e.token is None:
                results[e.company] = CompanyResult(company=e.company, status=CompanyStatus.NO_BOARD)
                continue
            if f.listing is None:
                assert f.error is not None
                results[e.company] = CompanyResult(
                    company=e.company, ats=e.ats, status=_status(f.error), error=str(f.error)
                )
                continue
            cache_hits += f.listing.cache_hit
            results[e.company] = CompanyResult(
                company=e.company, ats=e.ats, total=len(f.listing.jobs), status=CompanyStatus.OK
            )
            candidates.extend((e.company, j) for j in f.listing.jobs if prefilter(j, filters))

        enriched = await asyncio.gather(*(self._enrich_pay(j, refresh) for _, j in candidates))
        seen: set[str] = set()
        kept: list[Job] = []
        for (company, _), job in zip(candidates, enriched, strict=True):
            if job.id in seen or not passes_pay(job, filters):
                continue
            seen.add(job.id)
            if not include_description:
                job = job.model_copy(update={"description_text": None})
            kept.append(flag(job, filters))
            results[company].matched += 1

        jobs = sort_jobs(kept)
        flag_counts = Counter(fl for j in jobs for fl in j.flags)
        per_company = [results[n] for n in dict.fromkeys(order)]
        return SweepResult(
            jobs=jobs,
            per_company=per_company,
            fetched_at=self._now(),
            cache_hits=cache_hits,
            flag_counts=dict(sorted(flag_counts.items())),
        )

    async def _fetch_board(self, entry: WatchlistEntry, refresh: bool) -> _Fetch:
        result = _Fetch(entry)
        if entry.ats is None or entry.token is None:
            return result
        started = time.monotonic()
        try:
            result.listing = await ADAPTERS[entry.ats].list_jobs(
                self.http, entry.company, entry.token, refresh=refresh
            )
        except Exception as exc:  # one company never fails the sweep
            result.error = exc
            if not isinstance(exc, FetchError):
                log.exception("board fetch crashed", extra={"fields": {"company": entry.company}})
        result.latency_ms = round((time.monotonic() - started) * 1000)
        log.info(
            "board",
            extra={
                "fields": {
                    "company": entry.company,
                    "ats": entry.ats.value,
                    "status": _status(result.error).value if result.error else "ok",
                    "latency_ms": result.latency_ms,
                    "cache_hit": bool(result.listing and result.listing.cache_hit),
                    "jobs": len(result.listing.jobs) if result.listing else 0,
                }
            },
        )
        return result

    async def _enrich_pay(self, job: Job, refresh: bool) -> Job:
        adapter = ADAPTERS[job.ats]
        if not adapter.needs_detail_for_pay(job):
            return job
        try:
            detail = await adapter.get_job(
                self.http, job.company, job.board_token, job.native_id, refresh=refresh
            )
        except Exception as exc:  # keep the job; pay just stays unknown
            log.warning("pay detail failed", extra={"fields": {"job": job.id, "error": str(exc)}})
            return job
        return job.model_copy(update={k: getattr(detail, k) for k in _PAY_FIELDS})

    # ------------------------------------------------------------------ get_posting

    async def get_posting(
        self,
        *,
        ats: Ats | None = None,
        token: str | None = None,
        job_id: str | None = None,
        url: str | None = None,
        refresh: bool = False,
    ) -> Job:
        if url:
            ref = parse_posting_url(url)
            if ref is None:
                raise ValueError(f"not a recognized Greenhouse, Ashby, or Lever posting URL: {url}")
            ats, token, job_id = ref.ats, ref.token, ref.job_id
        if ats is None or not token or not job_id:
            raise ValueError("pass either url, or all of ats, token, and job_id")
        company = next(
            (e.company for e in self._watchlist() if e.ats is ats and e.token == token), token
        )
        return await ADAPTERS[ats].get_job(self.http, company, token, job_id, refresh=refresh)
