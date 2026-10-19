"""Workable accounts API: https://help.workable.com/hc/en-us/articles/115012771647

``https://www.workable.com/api/accounts/{subdomain}?details=true`` lists an account's published
jobs with their descriptions, with no key. Workable documents it for building job lists, so it's
tier 1. There's no per-job endpoint: the board payload holds everything.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from jobboard_mcp.adapters.base import (
    PAY_FIELDS,
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
from jobboard_mcp.normalize import (
    classify_remote,
    employment_type,
    html_to_text,
    parse_date,
    parse_pay_text,
)

API = "https://www.workable.com/api/accounts"
_SHORTCODE = re.compile(r"^[0-9A-F]{6,16}$", re.I)


def _location(loc: dict[str, Any]) -> str:
    """ "City, Region, Country", or just the country when the employer hid the place."""
    if loc.get("hidden"):
        return loc.get("country") or ""
    return ", ".join(p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)


class WorkableAdapter:
    ats = Ats.WORKABLE
    source = Source.DOCUMENTED_API
    guess_tokens = True
    detail_fields = PAY_FIELDS
    max_details: int | None = None

    def __init__(self) -> None:
        self._memo = ListingMemo()

    def list_url(self, token: str) -> str:
        return f"{API}/{token}?details=true"

    def to_job(self, raw: dict[str, Any], company: str, token: str) -> Job:
        title = raw.get("title") or ""
        places = raw.get("locations") or []
        locations = dedupe([_location(p) for p in places]) or dedupe(
            [", ".join(p for p in (raw.get("city"), raw.get("state"), raw.get("country")) if p)]
        )
        countries = dedupe([p.get("countryCode") or "" for p in places]) or dedupe(
            [raw.get("country") or ""]
        )
        # telecommuting is a yes/no: false can still mean hybrid, so it's not read as on-site.
        workplace = "remote" if raw.get("telecommuting") else "unspecified"
        published = raw.get("published_on") or raw.get("created_at")
        description = html_to_text(raw.get("description"))
        shortcode = raw.get("shortcode") or ""
        job = build_job(
            ats=self.ats,
            source=self.source,
            token=token.strip().lower(),
            native_id=shortcode.upper(),
            company=company,
            title=title,
            locations=locations,
            team=raw.get("department") or None,
            remote=classify_remote(locations, workplace, countries, title),
            employment_type=employment_type(raw.get("employment_type"), title),
            posted_at=parse_date(published),
            date_source=(
                "published_on" if raw.get("published_on") else "created_at" if published else None
            ),
            # Workable's own url is the short /j/{shortcode} form, which doesn't name the account.
            url=f"https://apply.workable.com/{token.strip().lower()}/j/{shortcode.upper()}/",
            apply_url=raw.get("application_url"),
            description_text=description,
        )
        job.set_pay(parse_pay_text(description))
        return job

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
        url = self.list_url(token)
        fetched = await http.get_json(url, refresh=refresh)
        jobs = self._memo.jobs(
            company,
            url,
            fetched.data,
            lambda: [
                self.to_job(j, company, token)
                for j in (fetched.data or {}).get("jobs", [])
                if _SHORTCODE.match(j.get("shortcode") or "")
            ],
        )
        return BoardListing(jobs, fetched.cache_hit)

    async def get_job(
        self, http: HttpClient, company: str, token: str, job_id: str, *, refresh: bool = False
    ) -> Job:
        listing = await self.list_jobs(http, company, token, refresh=refresh)
        for job in listing.jobs:
            if job.native_id == job_id.upper():
                return job
        raise NotFound(self.list_url(token), f"job {job_id} not on board {token}")

    async def check_live(
        self, http: HttpClient, token: str, job_id: str, *, refresh: bool = False
    ) -> LiveCheck:
        try:
            listing = await self.list_jobs(http, token, token, refresh=refresh)
        except NotFound:
            return LiveCheck(False, f"workable account {token!r} not found", False)
        if any(j.native_id == job_id.upper() for j in listing.jobs):
            return LiveCheck(True, "job is listed on the workable board", listing.cache_hit)
        return LiveCheck(False, "job is no longer listed on the workable board", listing.cache_hit)

    async def probe(self, http: HttpClient, token: str, *, refresh: bool = False) -> BoardProbe:
        data = (await http.get_json(self.list_url(token), refresh=refresh)).data
        jobs = data.get("jobs") if isinstance(data, dict) else None
        name = data.get("name") if isinstance(data, dict) else None
        return BoardProbe(len(jobs) if isinstance(jobs, list) else 0, {name} if name else set())

    def needs_detail(self, job: Job) -> bool:
        return False

    def parse_url(self, url: str) -> PostingRef | None:
        """``apply.workable.com/{account}/j/{shortcode}``. The short form
        ``apply.workable.com/j/{shortcode}`` doesn't name the account, so it isn't recognized."""
        parts = urlsplit(url)
        if (parts.hostname or "").lower() != "apply.workable.com":
            return None
        segs = [s for s in parts.path.split("/") if s]
        if len(segs) >= 3 and segs[1] == "j" and _SHORTCODE.match(segs[2]) and segs[0] != "j":
            return PostingRef(self.ats, segs[0].lower(), segs[2].upper())
        return None
