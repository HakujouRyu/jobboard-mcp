"""Workday careers sites (``*.myworkdayjobs.com``): tier 2 in ground rule 1.

Workday has no documented public jobs API. Each careers site loads its jobs from JSON
endpoints under ``/wday/cxs/{tenant}/{site}``, which need no sign-in and which robots.txt leaves
open (it disallows the human-facing pages instead). They're undocumented and can change without
notice; docs/api-notes.md records what they return. Requests go through ``get_site_json`` and
``post_site_json``, which keep them on public addresses, check robots.txt, and pace them.

A board token is ``{tenant}.{instance}/{site}``, as in ``homedepot.wd5/careerdepot``. Boards
can hold thousands of jobs, 20 to a page, so the list searches for the sweep's title terms and
stops at ``MAX_PER_SEARCH``. List rows have only a title, a location summary, and a relative
date, so every swept job gets a detail call (at most ``max_details`` per board).
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
    LiveCheck,
    PostingRef,
    build_job,
    dedupe,
)
from jobboard_mcp.http import FetchError, HttpClient, NotFound
from jobboard_mcp.models import Ats, Job, Source
from jobboard_mcp.normalize import (
    classify_remote,
    employment_type,
    html_to_text,
    parse_date,
    parse_pay_text,
    parse_relative_posted,
    workplace_from_label,
)

PAGE_SIZE = 20  # the most Workday serves per request
MAX_PER_SEARCH = 200  # jobs read per search term (10 requests)

_TOKEN = re.compile(r"^(?P<tenant>[a-z0-9][a-z0-9-]*)\.(?P<instance>wd\d{1,3})/(?P<site>[\w-]+)$")
_HOST = re.compile(r"^(?P<tenant>[a-z0-9][a-z0-9-]*)\.(?P<instance>wd\d{1,3})\.myworkdayjobs\.com$")
_SITE_HOST = re.compile(r"^(?P<instance>wd\d{1,3})\.myworkdaysite\.com$")
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_NAME = re.compile(r"^[\w-]+$")
_LOCATIONS_COUNT = re.compile(r"^\d+ Locations?$", re.I)
# Every Job field a detail call can improve on; identity fields stay as listed.
_DETAIL_FIELDS = frozenset(Job.model_fields) - {
    "id",
    "ats",
    "source",
    "board_token",
    "native_id",
    "company",
    "unverified_filters",
}


class WorkdayAdapter:
    ats = Ats.WORKDAY
    source = Source.CAREERS_SITE
    guess_tokens = False  # a token needs tenant, instance, and site: found from pages instead
    detail_fields = _DETAIL_FIELDS
    max_details: int | None = 20

    @staticmethod
    def _parts(token: str) -> tuple[str, str, str]:
        m = _TOKEN.match(token.strip().lower())
        if m is None:
            raise FetchError(
                f"workday:{token}", f"not a Workday board token (tenant.wdN/site): {token!r}"
            )
        return m["tenant"], m["instance"], m["site"]

    def _host(self, token: str) -> str:
        tenant, instance, _ = self._parts(token)
        return f"https://{tenant}.{instance}.myworkdayjobs.com"

    def _api(self, token: str) -> str:
        tenant, _, site = self._parts(token)
        return f"{self._host(token)}/wday/cxs/{tenant}/{site}"

    def list_url(self, token: str) -> str:
        return f"{self._api(token)}/jobs"

    def detail_url(self, token: str, job_id: str) -> str:
        return f"{self._api(token)}/job/{job_id}"

    def _public_url(self, token: str, external_path: str) -> str:
        _, _, site = self._parts(token)
        return f"{self._host(token)}/{site}{external_path}"

    async def _post(self, http: HttpClient, token: str, body: dict[str, Any], refresh: bool):
        try:
            return await http.post_site_json(self.list_url(token), body, refresh=refresh)
        except FetchError as exc:
            if exc.status == 422:  # Workday's answer for a tenant that doesn't exist
                raise NotFound(exc.url, "HTTP 422 (no such Workday tenant)", 422) from exc
            raise

    def _listed_job(
        self, row: dict[str, Any], company: str, token: str, today: date | None
    ) -> Job | None:
        path = row.get("externalPath") or ""
        native_id = path.rstrip("/").rsplit("/", 1)[-1]
        if not native_id:
            return None
        title = row.get("title") or ""
        summary = (row.get("locationsText") or "").strip()
        # "20 Locations" names none of them; the detail call lists them.
        locations = [] if not summary or _LOCATIONS_COUNT.match(summary) else [summary]
        posted = parse_relative_posted(row.get("postedOn"), today) if today else None
        return build_job(
            ats=self.ats,
            source=self.source,
            token=token,
            native_id=native_id,
            company=company,
            title=title,
            locations=locations,
            # List rows carry no remoteType, so only location text can say remote here.
            remote=classify_remote(locations, "unspecified", title=title),
            employment_type=employment_type(row.get("timeType"), title),
            posted_at=posted,
            date_source="postedOn" if posted else None,
            url=self._public_url(token, path),
        )

    def to_job(self, data: dict[str, Any], company: str, token: str) -> Job:
        info = data.get("jobPostingInfo") or {}
        title = info.get("title") or ""
        locations = dedupe([info.get("location") or "", *(info.get("additionalLocations") or [])])
        country = (info.get("country") or {}).get("descriptor")
        description = html_to_text(info.get("jobDescription"))
        started = info.get("startDate")
        native_id = info.get("jobPostingId") or info.get("externalUrl", "").rsplit("/", 1)[-1]
        job = build_job(
            ats=self.ats,
            source=self.source,
            token=token,
            native_id=native_id,
            company=company,
            title=title,
            locations=locations,
            remote=classify_remote(
                locations,
                workplace_from_label(info.get("remoteType")),
                [country] if country else [],
                title,
            ),
            employment_type=employment_type(info.get("timeType"), title),
            posted_at=parse_date(started),
            date_source="startDate" if started else None,
            url=info.get("externalUrl") or self._public_url(token, f"/job/{native_id}"),
            description_text=description,
        )
        job.set_pay(parse_pay_text(description))
        return job

    async def _search(
        self, http: HttpClient, term: str, token: str, refresh: bool
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Rows for one search, the total Workday reports, and whether all were cache hits."""
        rows: list[dict[str, Any]] = []
        total, cached, offset = 0, True, 0
        while offset < MAX_PER_SEARCH:
            body = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": term}
            fetched = await self._post(http, token, body, refresh)
            cached = cached and fetched.cache_hit
            page = fetched.data.get("jobPostings") if isinstance(fetched.data, dict) else None
            page = page if isinstance(page, list) else []
            if offset == 0:
                total = fetched.data.get("total") or 0
            rows.extend(page)
            offset += PAGE_SIZE
            if len(page) < PAGE_SIZE or offset >= total:
                break
        return rows, total, cached

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
        terms = list(dict.fromkeys(t.strip() for t in search if t.strip())) or [""]
        jobs: dict[str, Job] = {}
        capped: list[str] = []
        cached = True
        for term in terms:
            rows, total, hit = await self._search(http, term, token, refresh)
            cached = cached and hit
            if total > len(rows) and len(rows) >= MAX_PER_SEARCH:
                capped.append(f"{term!r}: {total}" if term else f"the whole board: {total}")
            for row in rows:
                job = self._listed_job(row, company, token, today)
                if job is not None:
                    jobs.setdefault(job.id, job)
        note = None
        if capped:
            note = (
                f"Workday searches read at most {MAX_PER_SEARCH} jobs each, and these had more"
                f" ({'; '.join(capped)}). Narrow title_include to see the rest."
            )
        return BoardListing(list(jobs.values()), cached, note)

    async def get_job(
        self, http: HttpClient, company: str, token: str, job_id: str, *, refresh: bool = False
    ) -> Job:
        fetched = await http.get_site_json(self.detail_url(token, job_id), refresh=refresh)
        return self.to_job(fetched.data, company, token)

    async def check_live(
        self, http: HttpClient, token: str, job_id: str, *, refresh: bool = False
    ) -> LiveCheck:
        try:
            fetched = await http.get_site_json(self.detail_url(token, job_id), refresh=refresh)
        except NotFound as exc:
            return LiveCheck(False, f"workday returned HTTP {exc.status} for the job", False)
        info = (fetched.data or {}).get("jobPostingInfo") or {}
        if info.get("posted") is False:
            return LiveCheck(False, "workday says the job is no longer posted", fetched.cache_hit)
        return LiveCheck(True, "workday job endpoint returned the posting", fetched.cache_hit)

    async def probe(self, http: HttpClient, token: str, *, refresh: bool = False) -> BoardProbe:
        body = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
        fetched = await self._post(http, token, body, refresh)
        data = fetched.data if isinstance(fetched.data, dict) else {}
        return BoardProbe(int(data.get("total") or 0), set())

    def needs_detail(self, job: Job) -> bool:
        return job.description_text is None  # list rows never have one

    def parse_url(self, url: str) -> PostingRef | None:
        """Posting URLs on ``{tenant}.wdN.myworkdayjobs.com`` (``/{site}/job/...``, with an
        optional locale, or the ``/wday/cxs/`` API form) and on ``wdN.myworkdaysite.com``
        (``/recruiting/{tenant}/{site}/job/...``). The last path segment is the job id."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        segs = [seg for seg in parts.path.split("/") if seg]
        if segs and segs[-1].lower() == "apply":
            segs.pop()
        if segs and _LOCALE.match(segs[0]):
            segs.pop(0)
        if m := _HOST.match(host):
            tenant, instance = m["tenant"], m["instance"]
            if segs[:2] == ["wday", "cxs"]:
                segs = segs[3:]  # drop "wday/cxs/{tenant}"; the host names the tenant
        elif m := _SITE_HOST.match(host):
            instance = m["instance"]
            if len(segs) < 2 or segs[0] != "recruiting":
                return None
            tenant, segs = segs[1].lower(), segs[2:]
        else:
            return None
        if len(segs) < 3 or segs[1] != "job" or not _NAME.match(segs[0]):
            return None
        return PostingRef(self.ats, f"{tenant}.{instance}/{segs[0].lower()}", segs[-1])
