"""Greenhouse job board API: https://developers.greenhouse.io/job-board.html"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlsplit

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
from jobboard_mcp.models import Ats, Job, Pay, PaySource, Source, raw_json
from jobboard_mcp.normalize import (
    PayRange,
    classify_remote,
    combine_ranges,
    employment_type,
    html_to_text,
    parse_date,
    parse_pay_text,
)

API = "https://boards-api.greenhouse.io/v1/boards"
_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"}
_EMPLOYMENT_META = re.compile(r"employment|job type|worker type|commitment", re.I)


def _structured_pay(ranges: list[dict[str, Any]]) -> Pay:
    """``pay_input_ranges`` are in cents with no interval; sub-$1,000 maxima are hourly."""
    parsed: list[PayRange] = []
    audit: list[dict[str, Any]] = []
    for r in ranges:
        lo = r.get("min_cents")
        hi = r.get("max_cents")
        lo_d = lo / 100 if isinstance(lo, int | float) else None
        hi_d = hi / 100 if isinstance(hi, int | float) else None
        top = hi_d if hi_d is not None else lo_d
        interval = "hour" if top is not None and top < 1000 else "year"
        parsed.append(PayRange(lo_d, hi_d, r.get("currency_type"), interval))
        audit.append({k: r.get(k) for k in ("title", "min_cents", "max_cents", "currency_type")})
    return combine_ranges(parsed, raw_json(audit), PaySource.STRUCTURED)


class GreenhouseAdapter:
    ats = Ats.GREENHOUSE
    source = Source.DOCUMENTED_API
    guess_tokens = True
    detail_fields = PAY_FIELDS
    max_details: int | None = None

    def __init__(self) -> None:
        self._memo = ListingMemo()

    def list_url(self, token: str) -> str:
        return f"{API}/{token}/jobs?content=true"

    def detail_url(self, token: str, job_id: str) -> str:
        return f"{API}/{token}/jobs/{job_id}?pay_transparency=true"

    def to_job(self, raw: dict[str, Any], company: str, token: str) -> Job:
        title = raw.get("title") or ""
        location = (raw.get("location") or {}).get("name") or ""
        locations = dedupe(location.split(";"))
        departments = raw.get("departments") or []
        meta = [m for m in raw.get("metadata") or [] if _EMPLOYMENT_META.search(m.get("name", ""))]
        meta_value = meta[0].get("value") if meta else None
        description = html_to_text(raw.get("content"))

        posted, source = raw.get("first_published"), "first_published"
        if not posted:
            posted, source = raw.get("updated_at"), "updated_at"

        ranges = raw.get("pay_input_ranges")
        pay = _structured_pay(ranges) if ranges else parse_pay_text(description)

        job = build_job(
            ats=self.ats,
            token=token,
            native_id=raw["id"],
            company=company,
            title=title,
            locations=locations,
            team=departments[0].get("name") if departments else None,
            remote=classify_remote(locations, None, title=title),
            employment_type=employment_type(
                meta_value if isinstance(meta_value, str) else None, title
            ),
            posted_at=parse_date(posted),
            updated_at=parse_date(raw.get("updated_at")),
            date_source=source if posted else None,
            url=raw.get("absolute_url")
            or f"https://job-boards.greenhouse.io/{token}/jobs/{raw['id']}",
            description_text=description,
        )
        job.set_pay(pay)
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
            lambda: [self.to_job(j, company, token) for j in fetched.data.get("jobs", [])],
        )
        return BoardListing(jobs, fetched.cache_hit)

    async def get_job(
        self, http: HttpClient, company: str, token: str, job_id: str, *, refresh: bool = False
    ) -> Job:
        fetched = await http.get_json(self.detail_url(token, job_id), refresh=refresh)
        return self.to_job(fetched.data, company, token)

    async def check_live(
        self, http: HttpClient, token: str, job_id: str, *, refresh: bool = False
    ) -> LiveCheck:
        try:
            fetched = await http.get_json(self.detail_url(token, job_id), refresh=refresh)
        except NotFound as exc:
            return LiveCheck(
                False, f"{self.ats.value} returned HTTP {exc.status} for the job", False
            )
        return LiveCheck(
            True, f"{self.ats.value} job endpoint returned the posting", fetched.cache_hit
        )

    async def probe(self, http: HttpClient, token: str, *, refresh: bool = False) -> BoardProbe:
        data = (await http.get_json(self.list_url(token), refresh=refresh)).data
        jobs = data.get("jobs") if isinstance(data, dict) else None
        jobs = jobs if isinstance(jobs, list) else []
        return BoardProbe(len(jobs), {j.get("company_name", "") for j in jobs[:20]} - {""})

    def needs_detail(self, job: Job) -> bool:
        return job.pay_source is PaySource.NONE

    def parse_url(self, url: str) -> PostingRef | None:
        parts = urlsplit(url)
        host = parts.netloc.lower()
        query = parse_qs(parts.query)
        if host == "boards-api.greenhouse.io":
            m = re.match(r"/v1/boards/([^/]+)/jobs/(\d+)", parts.path)
            return PostingRef(self.ats, m.group(1), m.group(2)) if m else None
        if host not in _HOSTS:
            return None
        # Embed form: /embed/job_app?for={token}&token={job_id}
        if "for" in query and "token" in query:
            return PostingRef(self.ats, query["for"][0], query["token"][0])
        m = re.match(r"/([^/]+)/jobs/(\d+)", parts.path)
        return PostingRef(self.ats, m.group(1), m.group(2)) if m else None
