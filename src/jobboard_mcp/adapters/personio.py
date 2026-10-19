"""Personio XML feed: https://developer.personio.de/docs/integration-of-open-positions

``https://{company}.jobs.personio.de/xml?language=en`` lists a company's published positions
with their descriptions, with no key. Personio documents it for putting open positions on the
employer's own site, so it's tier 1. There's no per-job endpoint: the feed holds everything.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import date
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
from jobboard_mcp.models import Ats, Job, Pay, PaySource, Source
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

DOMAIN = "jobs.personio.de"
_HOSTS = ("jobs.personio.de", "jobs.personio.com")  # the same feed on either
# The token becomes a hostname, so it must be a single DNS label.
_TOKEN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_JOB_ID = re.compile(r"^[0-9]{1,12}$")
_INTERN = {"intern", "trainee", "working_student"}
_CONTRACT = {"freelance", "fixed_term", "temporary"}


def _text(el: ET.Element, path: str) -> str:
    return (el.findtext(path) or "").strip()


def _float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _employment(pos: ET.Element) -> str | None:
    """employmentType is the contract (permanent, intern, fixed_term, ...) and schedule the
    hours. A permanent job's type comes from its schedule; "full-or-part-time" offers full
    time, so it counts as full time."""
    kind = _text(pos, "employmentType").lower()
    if kind in _INTERN:
        return "intern"
    if kind in _CONTRACT:
        return "contract"
    if kind in ("", "permanent"):
        schedule = _text(pos, "schedule").lower()
        return "full-time" if schedule == "full-or-part-time" else schedule or kind or None
    return kind


def _description(pos: ET.Element) -> str | None:
    parts = []
    for section in pos.findall("jobDescriptions/jobDescription"):
        body = html_to_text(section.findtext("value")) or ""
        parts.append(f"{_text(section, 'name')}\n{body}".strip())
    text = "\n\n".join(p for p in parts if p)
    return text or None


def _pay(pos: ET.Element, description: str | None) -> Pay:
    # salaryInformation isn't in Personio's reference, but the feed carries it (api-notes.md).
    salary = pos.find("salaryInformation")
    if salary is not None:
        lo, hi = _float(_text(salary, "min")), _float(_text(salary, "max"))
        if lo is not None or hi is not None:
            r = PayRange(
                lo,
                hi,
                _text(salary, "currencyCode") or None,
                interval_from_label(_text(salary, "type")),
            )
            raw = "; ".join(f"{c.tag}={(c.text or '').strip()}" for c in salary)
            return combine_ranges([r], raw, PaySource.STRUCTURED)
    return parse_pay_text(description)


def _host(token: str) -> str:
    name = token.strip().lower()
    if not _TOKEN.match(name):
        raise ValueError(f"{token!r} isn't a Personio company name (the part before .{DOMAIN})")
    return f"{name}.{DOMAIN}"


class PersonioAdapter:
    ats = Ats.PERSONIO
    source = Source.DOCUMENTED_API
    guess_tokens = True
    detail_fields = PAY_FIELDS
    max_details: int | None = None

    def __init__(self) -> None:
        self._memo = ListingMemo()

    def list_url(self, token: str) -> str:
        # English where the employer wrote it; otherwise Personio falls back to their language.
        return f"https://{_host(token)}/xml?language=en"

    def to_job(self, pos: ET.Element, company: str, token: str) -> Job:
        title = _text(pos, "name")
        offices = [pos.findtext("office") or ""]
        offices += [o.text or "" for o in pos.findall("additionalOffices/office")]
        locations = dedupe(offices)
        description = _description(pos)
        created = _text(pos, "createdAt")
        native_id = _text(pos, "id")
        name = token.strip().lower()
        job = build_job(
            ats=self.ats,
            source=self.source,
            token=name,
            native_id=native_id,
            company=company,
            title=title,
            locations=locations,
            team=_text(pos, "department") or None,
            # No workplace field: offices are free text, read like Greenhouse locations.
            remote=classify_remote(locations, None, title=title),
            employment_type=employment_type(_employment(pos), title),
            posted_at=parse_date(created or None),
            date_source="createdAt" if created else None,
            url=f"https://{name}.{DOMAIN}/job/{native_id}",
            description_text=description,
        )
        job.set_pay(_pay(pos, description))
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
        fetched = await http.get_xml(url, refresh=refresh)
        root: ET.Element = fetched.data
        jobs = self._memo.jobs(
            company,
            url,
            fetched.data,
            lambda: [
                self.to_job(p, company, token)
                for p in root.findall("position")
                if _JOB_ID.match(_text(p, "id"))
            ],
        )
        return BoardListing(jobs, fetched.cache_hit)

    async def get_job(
        self, http: HttpClient, company: str, token: str, job_id: str, *, refresh: bool = False
    ) -> Job:
        listing = await self.list_jobs(http, company, token, refresh=refresh)
        for job in listing.jobs:
            if job.native_id == job_id.strip():
                return job
        raise NotFound(self.list_url(token), f"job {job_id} not on board {token}")

    async def check_live(
        self, http: HttpClient, token: str, job_id: str, *, refresh: bool = False
    ) -> LiveCheck:
        try:
            listing = await self.list_jobs(http, token, token, refresh=refresh)
        except NotFound:
            return LiveCheck(False, f"personio company {token!r} not found", False)
        if any(j.native_id == job_id.strip() for j in listing.jobs):
            return LiveCheck(True, "job is listed in the personio feed", listing.cache_hit)
        return LiveCheck(False, "job is no longer listed in the personio feed", listing.cache_hit)

    async def probe(self, http: HttpClient, token: str, *, refresh: bool = False) -> BoardProbe:
        root: ET.Element = (await http.get_xml(self.list_url(token), refresh=refresh)).data
        positions = root.findall("position")
        # subcompany is the legal entity that's hiring ("Vivid Money GmbH").
        names = {s for p in positions if (s := _text(p, "subcompany"))}
        return BoardProbe(len(positions), names)

    def needs_detail(self, job: Job) -> bool:
        return False

    def parse_url(self, url: str) -> PostingRef | None:
        """``{company}.jobs.personio.de/job/{id}``, or the same on ``.com``."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        name, _, domain = host.partition(".")
        if domain not in _HOSTS or not _TOKEN.match(name):
            return None
        segs = [s for s in parts.path.split("/") if s]
        if len(segs) >= 2 and segs[0] == "job" and _JOB_ID.match(segs[1]):
            return PostingRef(self.ats, name, segs[1])
        return None
