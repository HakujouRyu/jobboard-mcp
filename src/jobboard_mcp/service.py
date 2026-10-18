"""Tool logic, kept apart from the MCP wiring so it can be tested directly.

A sweep runs in two phases: fetch every board in parallel and apply the cheap filters, then
fetch Greenhouse detail pages (pay transparency) only for the survivors that still lack pay.

The watchlist tools are the only writers, and they write only the server's own watchlist file.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from jobboard_mcp.adapters import ADAPTERS, parse_posting_url
from jobboard_mcp.adapters.base import BoardListing
from jobboard_mcp.config import Settings
from jobboard_mcp.filters import (
    SweepFilters,
    mark_unverified,
    passes_pay,
    prefilter,
    sort_jobs,
    term_hits,
)
from jobboard_mcp.http import FetchError, HttpClient, NotFound
from jobboard_mcp.matching import match_company, rank_postings
from jobboard_mcp.models import (
    Ats,
    BoardCheck,
    CompanyResult,
    CompanyStatus,
    FindResult,
    Job,
    LiveResult,
    Resolution,
    SweepResult,
    WatchlistChange,
    WatchlistEntry,
    WatchlistMatch,
    WatchlistView,
)
from jobboard_mcp.resolve import resolve_board
from jobboard_mcp.watchlist import WatchlistError, WatchlistStore

log = logging.getLogger(__name__)

_NO_CUSTOM_URLS = (
    "this server doesn't accept careers URLs from the client (ALLOW_CUSTOM_URLS=false); "
    "only the watchlist's careers_url is used"
)


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


_SUPPORTED = "Greenhouse, Ashby, Lever, Workable, Rippling, Personio, Workday, or Oracle"


async def _same(job: Job) -> Job:
    return job


def _join(*notes: str | None) -> str | None:
    text = "; ".join(n for n in notes if n)
    return text or None


class JobBoardService:
    def __init__(
        self,
        settings: Settings,
        http: HttpClient | None = None,
        *,
        store: WatchlistStore | None = None,
        watchlist: Callable[[], list[WatchlistEntry]] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """``watchlist`` overrides how entries are read (tests); edits always go to ``store``."""
        self.settings = settings
        self.http = http or HttpClient(settings)
        self.store = store or WatchlistStore(
            settings.watchlist_path, readonly=settings.watchlist_readonly
        )
        self._watchlist = watchlist or self.store.load
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
        limit: int | None = None,
    ) -> SweepResult:
        """``limit`` caps the jobs returned, newest first. Per-company ``matched`` counts and
        the result's ``matched`` count every job that passed the filters."""
        entries = self._watchlist()
        results: dict[str, CompanyResult] = {}
        # Archived companies are skipped unless asked for by name.
        order = [e.company for e in entries if not e.archived]  # per_company follows this order
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
        else:
            entries = [e for e in entries if not e.archived]

        fetches = await asyncio.gather(
            *(self._fetch_board(e, refresh, filters.title_include, filters.today) for e in entries)
        )
        cache_hits = 0
        candidates: list[tuple[str, Job, bool]] = []  # company, job, fetch its detail?
        titles: list[str] = []
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
            result = results[e.company] = CompanyResult(
                company=e.company,
                ats=e.ats,
                total=len(f.listing.jobs),
                status=CompanyStatus.OK,
                note=f.listing.note,
            )
            titles.extend(j.title for j in f.listing.jobs)
            adapter = ADAPTERS[e.ats]
            survivors = [j for j in f.listing.jobs if prefilter(j, filters)]
            wanted = [j.id for j in survivors if adapter.needs_detail(j)]
            cap = adapter.max_details
            if cap is not None and len(wanted) > cap:
                result.note = _join(
                    result.note,
                    f"details fetched for the first {cap} of {len(wanted)} matching jobs; the"
                    " rest show only what the board's list gives (narrow title_include to"
                    " cover them)",
                )
                wanted = wanted[:cap]
            detail = set(wanted)
            candidates.extend((e.company, j, j.id in detail) for j in survivors)

        enriched = await asyncio.gather(
            *(self._with_detail(j, refresh) if want else _same(j) for _, j, want in candidates)
        )
        seen: set[str] = set()
        kept: list[Job] = []
        for (company, _, _), job in zip(candidates, enriched, strict=True):
            # Filter again: a detail call can fill in remote status, date, or pay.
            if job.id in seen or not prefilter(job, filters) or not passes_pay(job, filters):
                continue
            seen.add(job.id)
            job = mark_unverified(job, filters)
            if not include_description:
                job = job.model_copy(update={"description_text": None})
            kept.append(job)
            results[company].matched += 1

        jobs = sort_jobs(kept)[:limit]
        flag_counts = Counter(fl for j in jobs for fl in j.flags)
        unverified_counts = Counter(n for j in jobs for n in j.unverified_filters)
        per_company = [results[n] for n in dict.fromkeys(order)]
        return SweepResult(
            jobs=jobs,
            matched=len(kept),
            truncated=len(jobs) < len(kept),
            per_company=per_company,
            fetched_at=self._now(),
            cache_hits=cache_hits,
            flag_counts=dict(sorted(flag_counts.items())),
            unverified_counts=dict(sorted(unverified_counts.items())),
            term_hits=term_hits(titles, filters),
        )

    async def _fetch_board(
        self, entry: WatchlistEntry, refresh: bool, search: Sequence[str], today: date
    ) -> _Fetch:
        result = _Fetch(entry)
        if entry.ats is None or entry.token is None:
            return result
        started = time.monotonic()
        try:
            result.listing = await ADAPTERS[entry.ats].list_jobs(
                self.http, entry.company, entry.token, refresh=refresh, search=search, today=today
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

    async def _with_detail(self, job: Job, refresh: bool) -> Job:
        adapter = ADAPTERS[job.ats]
        try:
            detail = await adapter.get_job(
                self.http, job.company, job.board_token, job.native_id, refresh=refresh
            )
        except Exception as exc:  # keep the job with what its listing had
            log.warning("detail failed", extra={"fields": {"job": job.id, "error": str(exc)}})
            return job
        return job.model_copy(update={k: getattr(detail, k) for k in adapter.detail_fields})

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
                raise ValueError(f"not a recognized {_SUPPORTED} posting URL: {url}")
            ats, token, job_id = ref.ats, ref.token, ref.job_id
        if ats is None or not token or not job_id:
            raise ValueError("pass either url, or all of ats, token, and job_id")
        return await ADAPTERS[ats].get_job(
            self.http, self._company_for(ats, token), token, job_id, refresh=refresh
        )

    def _company_for(self, ats: Ats, token: str) -> str:
        entry = self._entry_for_board(ats, token)
        return entry.company if entry else token

    def _entry_for_board(self, ats: Ats, token: str) -> WatchlistEntry | None:
        key = token.casefold()
        return next(
            (e for e in self._watchlist() if e.ats is ats and (e.token or "").casefold() == key),
            None,
        )

    def _entry(self, company: str) -> WatchlistEntry | None:
        """Exact name match, ignoring case. The write tools use only this."""
        key = company.strip().casefold()
        return next((e for e in self._watchlist() if e.company.casefold() == key), None)

    def _lookup(self, company: str) -> tuple[WatchlistEntry | None, WatchlistMatch | None]:
        """The watchlist entry for a name as another site shows it: an exact match, or else
        the one entry whose name starts with this one word for word (or the other way round),
        so "Temporal" finds "Temporal Technologies". Read tools only."""
        exact = self._entry(company)
        if exact is not None:
            return exact, WatchlistMatch.NAME
        entries = self._watchlist()
        name = match_company(company, [e.company for e in entries])
        if name is None:
            return None, None
        return next(e for e in entries if e.company == name), WatchlistMatch.LOOSE_NAME

    # ------------------------------------------------------------------ resolve / find

    async def resolve_board(
        self, company: str | None, careers_url: str | None = None, *, refresh: bool = False
    ) -> Resolution:
        """Read-only. Falls back to the watchlist's careers_url when none is given."""
        if careers_url and not self.settings.allow_custom_urls:
            raise ValueError(_NO_CUSTOM_URLS)
        if company and not careers_url:
            entry, _ = self._lookup(company)
            careers_url = entry.careers_url if entry else None
        return await resolve_board(self.http, company, careers_url, refresh=refresh)

    async def find_company_posting(
        self, company: str, title: str, location: str | None = None, *, refresh: bool = False
    ) -> FindResult:
        entry, how = self._lookup(company)
        name = entry.company if entry else company.strip()
        resolution: Resolution | None = None
        ats, token = (entry.ats, entry.token) if entry else (None, None)
        if ats is None or token is None:
            # The watchlist's URL is the operator's, so this skips the custom-URL check.
            careers_url = entry.careers_url if entry else None
            resolution = await resolve_board(self.http, name, careers_url)
            ats, token = resolution.ats, resolution.token
            if entry is None and ats is not None and token is not None:
                # A board the watchlist already has: use its name, so fingerprints match a sweep.
                owner = self._entry_for_board(ats, token)
                if owner is not None:
                    name, how = owner.company, WatchlistMatch.BOARD
        result = FindResult(
            company=name, title=title, watchlist_match=how, board_found=False, resolution=resolution
        )
        if ats is None or token is None:
            return result
        try:
            listing = await ADAPTERS[ats].list_jobs(
                self.http, name, token, refresh=refresh, search=[title], today=self.today()
            )
        except NotFound:
            return result
        result.board_found, result.ats, result.token = True, ats, token
        result.candidates = rank_postings(listing.jobs, title, location)
        return result

    # ------------------------------------------------------------------ liveness

    async def is_posting_live(
        self,
        *,
        url: str | None = None,
        ats: Ats | None = None,
        token: str | None = None,
        job_id: str | None = None,
        refresh: bool = False,
    ) -> LiveResult:
        if url:
            ref = parse_posting_url(url)
            if ref is None:
                raise ValueError(f"not a recognized {_SUPPORTED} posting URL: {url}")
            ats, token, job_id = ref.ats, ref.token, ref.job_id
        if ats is None or not token or not job_id:
            raise ValueError("pass either url, or all of ats, token, and job_id")
        check = await ADAPTERS[ats].check_live(self.http, token, job_id, refresh=refresh)
        return LiveResult(
            live=check.live,
            checked_at=self._now(),
            reason=check.reason,
            ats=ats,
            token=token,
            job_id=job_id,
            from_cache=check.cache_hit,
        )

    # ------------------------------------------------------------------ watchlist

    def list_watchlist(
        self,
        *,
        has_board: bool | None = None,
        tag: str | None = None,
        domain: str | None = None,
        include_archived: bool = False,
    ) -> WatchlistView:
        entries = self._watchlist()
        hidden = 0
        if not include_archived:
            hidden = sum(e.archived for e in entries)
            entries = [e for e in entries if not e.archived]
        if has_board is not None:
            entries = [e for e in entries if (e.ats is not None) is has_board]
        if tag:
            entries = [e for e in entries if tag.casefold() in (t.casefold() for t in e.tags)]
        if domain:
            d = domain.casefold()
            entries = [e for e in entries if any(d in x.casefold() for x in e.domains)]
        return WatchlistView(
            companies=entries,
            total=len(entries),
            readonly=self.store.readonly,
            path=str(self.store.path),
            archived_hidden=hidden,
        )

    async def _check_board(self, entry: WatchlistEntry) -> BoardCheck:
        assert entry.ats is not None and entry.token is not None
        try:
            listing = await ADAPTERS[entry.ats].list_jobs(
                self.http, entry.company, entry.token, refresh=True
            )
        except FetchError as exc:
            return BoardCheck(status=CompanyStatus(exc.kind), error=str(exc))
        return BoardCheck(status=CompanyStatus.OK, jobs=len(listing.jobs))

    def _require_writable(self) -> None:
        if self.store.readonly:
            raise WatchlistError("watchlist is read-only on this server (WATCHLIST_READONLY)")

    async def _validated(self, entry: WatchlistEntry) -> BoardCheck:
        assert entry.ats is not None and entry.token is not None
        check = await self._check_board(entry)
        if check.status is not CompanyStatus.OK:
            raise WatchlistError(
                f"board {entry.ats.value}:{entry.token} failed validation: "
                f"{check.status.value} ({check.error})"
            )
        return check

    async def add_company(self, entry: WatchlistEntry) -> WatchlistChange:
        """Validate the board with a live fetch, then append. Nothing is saved on failure."""
        self._require_writable()
        if entry.careers_url and not self.settings.allow_custom_urls:
            raise WatchlistError(_NO_CUSTOM_URLS)
        existing = self._entry(entry.company)
        if existing is not None:
            hint = " (archived; restore it with update_company)" if existing.archived else ""
            raise WatchlistError(f"{entry.company!r} is already on the watchlist{hint}")
        check = None
        if entry.ats is not None and entry.token is not None:
            check = await self._validated(entry)
        return WatchlistChange(entry=self.store.add(entry), board_check=check)

    async def update_company(self, company: str, changes: Mapping[str, Any]) -> WatchlistChange:
        """Partial update. A new or changed board is validated live before saving."""
        self._require_writable()
        # Clearing careers_url is fine; setting one isn't.
        if changes.get("careers_url") and not self.settings.allow_custom_urls:
            raise WatchlistError(_NO_CUSTOM_URLS)
        current = self._entry(company)
        if current is None:
            raise WatchlistError(f"{company!r} is not on the watchlist")
        merged = WatchlistEntry.model_validate({**current.model_dump(), **changes})
        check = None
        board_changed = (merged.ats, merged.token) != (current.ats, current.token)
        if board_changed and merged.ats is not None and merged.token is not None:
            check = await self._validated(merged)
        saved = self.store.update(current.company, changes)
        return WatchlistChange(entry=saved, board_check=check)
