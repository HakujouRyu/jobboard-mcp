"""Lever postings API: https://github.com/lever/postings-api"""

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
    html_to_text,
    interval_from_label,
    parse_date,
    parse_pay_text,
)

API = "https://api.lever.co/v0/postings"


def _description(raw: dict[str, Any]) -> str | None:
    parts: list[str] = [raw.get("descriptionPlain") or ""]
    for section in raw.get("lists") or []:
        body = html_to_text(section.get("content")) or ""
        parts.append(f"{section.get('text', '')}\n{body}".strip())
    parts.append(raw.get("additionalPlain") or "")
    text = "\n\n".join(p.strip() for p in parts if p and p.strip())
    return text or None


def _pay(raw: dict[str, Any], description: str | None) -> Pay:
    salary = raw.get("salaryRange")
    if salary and (salary.get("min") is not None or salary.get("max") is not None):
        r = PayRange(
            salary.get("min"),
            salary.get("max"),
            salary.get("currency"),
            interval_from_label(salary.get("interval")),
        )
        return combine_ranges([r], raw_json(salary), PaySource.STRUCTURED)
    for text in (raw.get("salaryDescriptionPlain"), description):
        pay = parse_pay_text(text)
        if pay.source is not PaySource.NONE:
            return pay
    return Pay()


class LeverAdapter:
    ats = Ats.LEVER

    def __init__(self) -> None:
        self._memo = ListingMemo()

    def list_url(self, token: str) -> str:
        return f"{API}/{token}?mode=json"

    def detail_url(self, token: str, job_id: str) -> str:
        return f"{API}/{token}/{job_id}?mode=json"

    def to_job(self, raw: dict[str, Any], company: str, token: str) -> Job:
        cats = raw.get("categories") or {}
        locations = dedupe(cats.get("allLocations") or [cats.get("location") or ""])
        created = raw.get("createdAt")
        description = _description(raw)

        job = build_job(
            ats=self.ats,
            token=token,
            native_id=raw["id"],
            company=company,
            title=raw.get("text") or "",
            locations=locations,
            team=cats.get("team") or cats.get("department"),
            remote=classify_remote(locations, raw.get("workplaceType"), [raw.get("country") or ""]),
            employment_type=employment_type(cats.get("commitment")),
            posted_at=parse_date(created),
            date_source="createdAt" if created is not None else None,
            url=raw.get("hostedUrl") or f"https://jobs.lever.co/{token}/{raw['id']}",
            apply_url=raw.get("applyUrl"),
            description_text=description,
        )
        job.set_pay(_pay(raw, description))
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
            lambda: [self.to_job(j, company, token) for j in fetched.data],
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

    def needs_detail_for_pay(self, job: Job) -> bool:
        return False

    def parse_url(self, url: str) -> PostingRef | None:
        parts = urlsplit(url)
        if parts.netloc.lower() != "jobs.lever.co":
            return None
        m = re.match(r"/([^/]+)/([0-9a-fA-F-]{36})", parts.path)
        return PostingRef(self.ats, m.group(1), m.group(2).lower()) if m else None
