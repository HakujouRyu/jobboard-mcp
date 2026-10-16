"""Ashby public posting API: https://developers.ashbyhq.com/docs/public-job-posting-api"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from jobboard_mcp.adapters.base import (
    BoardListing,
    ListingMemo,
    LiveCheck,
    PostingRef,
    build_job,
    dedupe,
)
from jobboard_mcp.http import HttpClient, NotFound
from jobboard_mcp.models import Ats, Job, Pay, PaySource, raw_json
from jobboard_mcp.normalize import (
    PayRange,
    classify_remote,
    combine_ranges,
    employment_type,
    interval_from_label,
    parse_date,
    parse_pay_text,
)

API = "https://api.ashbyhq.com/posting-api/job-board"


def _country(address: dict[str, Any] | None) -> str:
    return ((address or {}).get("postalAddress") or {}).get("addressCountry") or ""


def _pay(raw: dict[str, Any]) -> Pay:
    """Salary components only; bonus, commission, and equity are not base pay."""
    comp = raw.get("compensation") or {}
    ranges: list[PayRange] = []
    audit: list[dict[str, Any]] = []
    for tier in comp.get("compensationTiers") or []:
        for c in tier.get("components") or []:
            if c.get("compensationType") != "Salary":
                continue
            ranges.append(
                PayRange(
                    c.get("minValue"),
                    c.get("maxValue"),
                    c.get("currencyCode"),
                    interval_from_label(c.get("interval")),
                )
            )
            audit.append({"tier": tier.get("title"), "summary": c.get("summary")})
    if ranges:
        return combine_ranges(ranges, raw_json(audit), PaySource.STRUCTURED)
    for text in (
        comp.get("scrapeableCompensationSalarySummary"),
        comp.get("compensationTierSummary"),
        raw.get("descriptionPlain"),
    ):
        pay = parse_pay_text(text)
        if pay.source is not PaySource.NONE:
            return pay
    return Pay()


class AshbyAdapter:
    ats = Ats.ASHBY

    def __init__(self) -> None:
        self._memo = ListingMemo()

    def list_url(self, token: str) -> str:
        return f"{API}/{token}?includeCompensation=true"

    def to_job(self, raw: dict[str, Any], company: str, token: str) -> Job:
        title = raw.get("title") or ""
        secondary = raw.get("secondaryLocations") or []
        locations = dedupe([raw.get("location") or "", *(s.get("location", "") for s in secondary)])
        countries = dedupe(
            [_country(raw.get("address")), *(_country(s.get("address")) for s in secondary)]
        )
        workplace = raw.get("workplaceType")
        if not workplace and raw.get("isRemote"):
            workplace = "remote"  # isRemote is unreliable; only a fallback (docs/api-notes.md)
        published = raw.get("publishedAt")

        job = build_job(
            ats=self.ats,
            token=token,
            native_id=raw["id"],
            company=company,
            title=title,
            locations=locations,
            team=raw.get("team") or raw.get("department"),
            remote=classify_remote(locations, workplace, countries, title),
            employment_type=employment_type(raw.get("employmentType"), title),
            posted_at=parse_date(published),
            date_source="publishedAt" if published else None,
            url=raw.get("jobUrl") or f"https://jobs.ashbyhq.com/{token}/{raw['id']}",
            apply_url=raw.get("applyUrl"),
            description_text=raw.get("descriptionPlain") or None,
        )
        job.set_pay(_pay(raw))
        return job

    async def list_jobs(
        self, http: HttpClient, company: str, token: str, *, refresh: bool = False
    ) -> BoardListing:
        url = self.list_url(token)
        fetched = await http.get_json(url, refresh=refresh)
        jobs = self._memo.jobs(
            company,
            url,
            fetched.data,
            lambda: [
                self.to_job(j, company, token)
                for j in fetched.data.get("jobs", [])
                if j.get("isListed", True)
            ],
        )
        return BoardListing(jobs, fetched.cache_hit)

    async def get_job(
        self, http: HttpClient, company: str, token: str, job_id: str, *, refresh: bool = False
    ) -> Job:
        # No public detail endpoint: the board payload already holds everything.
        listing = await self.list_jobs(http, company, token, refresh=refresh)
        for job in listing.jobs:
            if job.native_id == job_id:
                return job
        raise NotFound(self.list_url(token), f"job {job_id} not on board {token}")

    async def check_live(
        self, http: HttpClient, token: str, job_id: str, *, refresh: bool = False
    ) -> LiveCheck:
        # No per-job endpoint: look for the job on the (usually cached) board.
        try:
            listing = await self.list_jobs(http, token, token, refresh=refresh)
        except NotFound:
            return LiveCheck(False, f"ashby board {token!r} not found", False)
        if any(j.native_id == job_id.lower() for j in listing.jobs):
            return LiveCheck(True, "job is listed on the ashby board", listing.cache_hit)
        return LiveCheck(False, "job is no longer listed on the ashby board", listing.cache_hit)

    def needs_detail_for_pay(self, job: Job) -> bool:
        return False

    def parse_url(self, url: str) -> PostingRef | None:
        parts = urlsplit(url)
        if parts.netloc.lower() != "jobs.ashbyhq.com":
            return None
        m = re.match(r"/([^/]+)/([0-9a-fA-F-]{36})", parts.path)
        return PostingRef(self.ats, m.group(1), m.group(2).lower()) if m else None
