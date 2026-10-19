"""Rippling job board API (v2): https://developer.rippling.com/documentation/job-board-api

``https://api.rippling.com/platform/api/ats/v2/board/{slug}/jobs`` lists a board's published
jobs with no key. Rippling documents it for rendering a job board on the employer's own site, so
it's tier 1. Each job has a title, department, and locations with a structured workplace type,
but no description, pay, posting date, or employment type, and there's no documented per-job
endpoint. The per-job JSON that does exist is undocumented and the careers site doesn't load it
(job pages are rendered on the server), so it isn't read. See docs/api-notes.md.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from jobboard_mcp.adapters.base import (
    BoardListing,
    BoardProbe,
    ListingMemo,
    LiveCheck,
    PostingRef,
    build_job,
    dedupe,
)
from jobboard_mcp.http import HttpClient, NotFound
from jobboard_mcp.models import Ats, Job, Source
from jobboard_mcp.normalize import classify_remote, employment_type

API = "https://api.rippling.com/platform/api/ats/v2/board"
PAGE_SIZE = 1000  # the documented maximum; one page covers nearly every board
MAX_PAGES = 5
_JOB_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_LOCALE = re.compile(r"^[a-z]{2}-[a-z]{2}$", re.I)


def _workplace(places: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """The job's workplace type, and the locations that decide where it's open.

    Each location has its own type. A job with any remote location can be done remotely from
    those places, so only they count toward its region: on-site in San Francisco or remote in
    the Philippines is ``remote_other``, not ``remote_us``.
    """
    types = {(p.get("workplaceType") or "").upper() for p in places}
    remote = [p for p in places if (p.get("workplaceType") or "").upper() == "REMOTE"]
    if remote:
        return "remote", remote
    if "HYBRID" in types:
        return "hybrid", places
    if types == {"ON_SITE"}:
        return "onsite", places
    return "unspecified", places


class RipplingAdapter:
    ats = Ats.RIPPLING
    source = Source.DOCUMENTED_API
    guess_tokens = True
    detail_fields: frozenset[str] = frozenset()
    max_details: int | None = None

    def __init__(self) -> None:
        self._memo = ListingMemo()

    def list_url(self, token: str, page: int = 0) -> str:
        # groupJobsByLocation=true gives one row per job; without it, one per job and location.
        url = f"{API}/{token.strip()}/jobs?pageSize={PAGE_SIZE}&groupJobsByLocation=true"
        return f"{url}&page={page}" if page else url

    def to_job(self, raw: dict[str, Any], company: str, token: str) -> Job:
        title = raw.get("name") or ""
        places = [p for p in raw.get("locations") or [] if isinstance(p, dict)]
        workplace, evidence = _workplace(places)
        department = raw.get("department")
        return build_job(
            ats=self.ats,
            source=self.source,
            token=token.strip(),
            native_id=(raw.get("id") or "").lower(),
            company=company,
            title=title,
            locations=dedupe([p.get("name") or "" for p in places]),
            team=(department.get("name") if isinstance(department, dict) else None) or None,
            remote=classify_remote(
                dedupe([p.get("name") or "" for p in evidence]),
                workplace,
                dedupe([p.get("countryCode") or "" for p in evidence]),
                title,
            ),
            employment_type=employment_type(None, title),
            url=raw.get("url"),
        )

    async def _page(
        self, http: HttpClient, company: str, token: str, page: int, refresh: bool
    ) -> tuple[list[Job], int, bool]:
        """One page's jobs, the number of pages Rippling reports, and whether it was cached."""
        url = self.list_url(token, page)
        fetched = await http.get_json(url, refresh=refresh)
        data = fetched.data if isinstance(fetched.data, dict) else {}
        jobs = self._memo.jobs(
            company,
            url,
            fetched.data,
            lambda: [
                self.to_job(j, company, token)
                for j in data.get("items") or []
                if isinstance(j, dict) and _JOB_ID.match(j.get("id") or "")
            ],
        )
        pages = data.get("totalPages")
        return jobs, pages if isinstance(pages, int) else 0, fetched.cache_hit

    async def list_jobs(
        self,
        http: HttpClient,
        company: str,
        token: str,
        *,
        refresh: bool = False,
        search: Sequence[str] = (),
        today: date | None = None,
    ) -> BoardListing:
        jobs: dict[str, Job] = {}
        page_jobs, pages, cached = await self._page(http, company, token, 0, refresh)
        for job in page_jobs:
            jobs.setdefault(job.id, job)
        for page in range(1, min(pages, MAX_PAGES)):
            page_jobs, _, hit = await self._page(http, company, token, page, refresh)
            cached = cached and hit
            # Rippling returns jobs in no set order, so pages can overlap.
            for job in page_jobs:
                jobs.setdefault(job.id, job)
        note = None
        if pages > MAX_PAGES:
            note = (
                f"Rippling boards are read up to {MAX_PAGES * PAGE_SIZE} jobs, and this one has"
                f" {pages} pages of {PAGE_SIZE}, so the rest aren't included."
            )
        return BoardListing(list(jobs.values()), cached, note)

    async def get_job(
        self, http: HttpClient, company: str, token: str, job_id: str, *, refresh: bool = False
    ) -> Job:
        # No documented per-job endpoint, and the list holds all the data there is.
        listing = await self.list_jobs(http, company, token, refresh=refresh)
        for job in listing.jobs:
            if job.native_id == job_id.lower():
                return job
        raise NotFound(self.list_url(token), f"job {job_id} not on board {token}")

    async def check_live(
        self, http: HttpClient, token: str, job_id: str, *, refresh: bool = False
    ) -> LiveCheck:
        try:
            listing = await self.list_jobs(http, token, token, refresh=refresh)
        except NotFound:
            return LiveCheck(False, f"rippling board {token!r} not found", False)
        if any(j.native_id == job_id.lower() for j in listing.jobs):
            return LiveCheck(True, "job is listed on the rippling board", listing.cache_hit)
        return LiveCheck(False, "job is no longer listed on the rippling board", listing.cache_hit)

    async def probe(self, http: HttpClient, token: str, *, refresh: bool = False) -> BoardProbe:
        url = f"{API}/{token.strip()}/jobs?pageSize=1&groupJobsByLocation=true"
        data = (await http.get_json(url, refresh=refresh)).data
        total = data.get("totalItems") if isinstance(data, dict) else None
        return BoardProbe(total if isinstance(total, int) else 0, set())

    def needs_detail(self, job: Job) -> bool:
        return False

    def parse_url(self, url: str) -> PostingRef | None:
        """``ats.rippling.com/{slug}/jobs/{uuid}``, with an optional locale before the slug
        (``/en-GB/``) and anything after the id (``/apply``)."""
        parts = urlsplit(url)
        if (parts.hostname or "").lower() != "ats.rippling.com":
            return None
        segs = [s for s in parts.path.split("/") if s]
        if segs and _LOCALE.match(segs[0]):
            segs = segs[1:]
        if len(segs) >= 3 and segs[1] == "jobs" and _JOB_ID.match(segs[2]):
            return PostingRef(self.ats, segs[0], segs[2].lower())
        return None
