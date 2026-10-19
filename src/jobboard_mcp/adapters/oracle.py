"""Oracle Recruiting Cloud careers sites (``*.fa.*.oraclecloud.com``): tier 2 in ground rule 1.

Oracle publishes no public jobs API for its Candidate Experience sites. Each site loads its
jobs from the HCM REST resources ``recruitingCEJobRequisitions`` (search) and
``recruitingCEJobRequisitionDetails`` (one job), which need no sign-in. They're undocumented for
this use and can change without notice; docs/api-notes.md records what they return. Requests go
through ``get_site_json``, which keeps them on public addresses, checks robots.txt, and paces them.

A board token is ``{pod}.fa.{region}/{site number}``, as in ``ehxr.fa.us2/CX_1`` (City of
Atlanta). Search rows already carry the title, locations, country, workplace type, and posting
date, so a swept job's detail call adds only its description and any pay in it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from typing import Any
from urllib.parse import quote, urlsplit

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
    workplace_from_label,
)

PAGE_SIZE = 200  # the most Oracle serves per request
MAX_PER_SEARCH = 200  # jobs read per search term (one request)

_TOKEN = re.compile(
    r"^(?P<pod>[a-z0-9][a-z0-9-]*)\.fa\.(?P<region>[a-z0-9]+)/(?P<site>[a-z0-9_-]+)$", re.I
)
_HOST = re.compile(r"^(?P<pod>[a-z0-9][a-z0-9-]*)\.fa\.(?P<region>[a-z0-9]+)\.oraclecloud\.com$")
_ID = re.compile(r"^\d{1,20}$")
_LANG = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2})?$")
# Keywords go inside a quoted finder argument; keep only characters that can't end it.
_KEYWORD_UNSAFE = re.compile(r"[^\w\s.+&'/-]")
_REST = "/hcmRestApi/resources/latest"
_DESCRIPTION_FIELDS = (
    "ExternalDescriptionStr",
    "ExternalResponsibilitiesStr",
    "ExternalQualificationsStr",
)
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


def _locations(row: dict[str, Any]) -> list[str]:
    secondary = row.get("secondaryLocations") or []
    return dedupe([row.get("PrimaryLocation") or "", *(s.get("Name") or "" for s in secondary)])


def _countries(row: dict[str, Any]) -> list[str]:
    secondary = row.get("secondaryLocations") or []
    return dedupe(
        [row.get("PrimaryLocationCountry") or "", *(s.get("CountryCode") or "" for s in secondary)]
    )


def _workplace(row: dict[str, Any]) -> str:
    return workplace_from_label(row.get("WorkplaceType") or row.get("WorkplaceTypeCode"))


class OracleAdapter:
    ats = Ats.ORACLE
    source = Source.CAREERS_SITE
    guess_tokens = False  # a token needs pod, region, and site number: found from pages instead
    detail_fields = _DETAIL_FIELDS
    max_details: int | None = 20

    @staticmethod
    def _parts(token: str) -> tuple[str, str]:
        """(host, site number)."""
        m = _TOKEN.match(token.strip())
        if m is None:
            raise FetchError(
                f"oracle:{token}",
                f"not an Oracle Recruiting Cloud board token (pod.fa.region/site): {token!r}",
            )
        site = m["site"]
        if site.lower().startswith("cx"):
            site = site.upper()  # CX, CX_1, CX_45001
        return f"{m['pod'].lower()}.fa.{m['region'].lower()}.oraclecloud.com", site

    def list_url(self, token: str, keyword: str | None = None, limit: int = PAGE_SIZE) -> str:
        host, site = self._parts(token)
        finder = f"findReqs;siteNumber={site},limit={limit},offset=0,sortBy=POSTING_DATES_DESC"
        if keyword:
            # An empty keyword finds nothing, so it's left out to list the whole site.
            finder += f',keyword="{keyword}"'
        return (
            f"https://{host}{_REST}/recruitingCEJobRequisitions?onlyData=true"
            f"&expand=requisitionList.secondaryLocations&finder={quote(finder, safe='=;,')}"
        )

    def detail_url(self, token: str, job_id: str) -> str:
        host, site = self._parts(token)
        if not _ID.match(job_id):
            raise NotFound(f"oracle:{token}:{job_id}", f"not an Oracle job id: {job_id!r}")
        finder = f'ById;Id="{job_id}",siteNumber={site}'
        return (
            f"https://{host}{_REST}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
            f"&finder={quote(finder, safe='=;,')}"
        )

    def public_url(self, token: str, job_id: str) -> str:
        host, site = self._parts(token)
        return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}"

    def _listed_job(self, row: dict[str, Any], company: str, token: str) -> Job | None:
        job_id = str(row.get("Id") or "")
        if not _ID.match(job_id):
            return None
        title = row.get("Title") or ""
        locations = _locations(row)
        posted = row.get("PostedDate")
        return build_job(
            ats=self.ats,
            source=self.source,
            token=token.strip().lower(),  # one id however the token is cased
            native_id=job_id,
            company=company,
            title=title,
            locations=locations,
            remote=classify_remote(locations, _workplace(row), _countries(row), title),
            employment_type=employment_type(row.get("JobSchedule"), title),
            posted_at=parse_date(posted),
            date_source="PostedDate" if posted else None,
            url=self.public_url(token, job_id),
        )

    def to_job(self, row: dict[str, Any], company: str, token: str) -> Job:
        job_id = str(row.get("Id") or "")
        title = row.get("Title") or ""
        locations = _locations(row)
        html = "\n".join(row.get(k) or "" for k in _DESCRIPTION_FIELDS)
        description = html_to_text(html) if html.strip() else None
        posted = row.get("ExternalPostedStartDate")
        job = build_job(
            ats=self.ats,
            source=self.source,
            token=token.strip().lower(),  # one id however the token is cased
            native_id=job_id,
            company=company,
            title=title,
            locations=locations,
            team=row.get("Category") or row.get("Department"),
            remote=classify_remote(locations, _workplace(row), _countries(row), title),
            employment_type=employment_type(row.get("JobSchedule"), title),
            posted_at=parse_date(posted),
            date_source="ExternalPostedStartDate" if posted else None,
            url=self.public_url(token, job_id),
            description_text=description,
        )
        job.set_pay(parse_pay_text(description))
        return job

    async def _search(
        self, http: HttpClient, token: str, keyword: str | None, limit: int, refresh: bool
    ) -> tuple[list[dict[str, Any]], int, bool]:
        fetched = await http.get_site_json(self.list_url(token, keyword, limit), refresh=refresh)
        items = fetched.data.get("items") if isinstance(fetched.data, dict) else None
        item = items[0] if isinstance(items, list) and items else {}
        rows = item.get("requisitionList") or []
        return rows, int(item.get("TotalJobsCount") or 0), fetched.cache_hit

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
        terms = list(
            dict.fromkeys(
                " ".join(_KEYWORD_UNSAFE.sub(" ", t).split()) for t in search if t.strip()
            )
        )
        terms = [t for t in terms if t] or [None]
        jobs: dict[str, Job] = {}
        capped: list[str] = []
        cached = True
        for term in terms:
            rows, total, hit = await self._search(http, token, term, PAGE_SIZE, refresh)
            cached = cached and hit
            if total > len(rows) and len(rows) >= MAX_PER_SEARCH:
                capped.append(f"{term!r}: {total}" if term else f"the whole board: {total}")
            for row in rows:
                job = self._listed_job(row, company, token)
                if job is not None:
                    jobs.setdefault(job.id, job)
        note = None
        if capped:
            note = (
                f"Oracle searches read at most {MAX_PER_SEARCH} jobs each, newest first, and"
                f" these had more ({'; '.join(capped)}). Narrow title_include to see the rest."
            )
        return BoardListing(list(jobs.values()), cached, note)

    async def _detail(
        self, http: HttpClient, token: str, job_id: str, refresh: bool
    ) -> tuple[dict[str, Any], bool]:
        fetched = await http.get_site_json(self.detail_url(token, job_id), refresh=refresh)
        items = fetched.data.get("items") if isinstance(fetched.data, dict) else None
        if not isinstance(items, list) or not items:
            # Oracle answers 200 with no items for a job that isn't posted.
            raise NotFound(self.detail_url(token, job_id), f"job {job_id} not found", 404)
        return items[0], fetched.cache_hit

    async def get_job(
        self, http: HttpClient, company: str, token: str, job_id: str, *, refresh: bool = False
    ) -> Job:
        row, _ = await self._detail(http, token, job_id, refresh)
        return self.to_job(row, company, token)

    async def check_live(
        self, http: HttpClient, token: str, job_id: str, *, refresh: bool = False
    ) -> LiveCheck:
        try:
            _, cached = await self._detail(http, token, job_id, refresh)
        except NotFound:
            return LiveCheck(False, "oracle returned no posting for the job", False)
        return LiveCheck(True, "oracle job endpoint returned the posting", cached)

    async def probe(self, http: HttpClient, token: str, *, refresh: bool = False) -> BoardProbe:
        _, total, _ = await self._search(http, token, None, 1, refresh)
        return BoardProbe(total, set())

    def needs_detail(self, job: Job) -> bool:
        return job.description_text is None  # search rows never have one

    def parse_url(self, url: str) -> PostingRef | None:
        """``{pod}.fa.{region}.oraclecloud.com/hcmUI/CandidateExperience/{lang}/sites/{site}/
        job/{id}``, with or without the language segment, and with anything after the id."""
        parts = urlsplit(url)
        m = _HOST.match((parts.hostname or "").lower())
        if m is None:
            return None
        segs = [s for s in parts.path.split("/") if s]
        if segs[:2] != ["hcmUI", "CandidateExperience"]:
            return None
        segs = segs[2:]
        if segs and _LANG.match(segs[0]) and segs[0] != "sites":
            segs = segs[1:]
        if len(segs) < 4 or segs[0] != "sites" or segs[2] != "job" or not _ID.match(segs[3]):
            return None
        return PostingRef(self.ats, f"{m['pod']}.fa.{m['region']}/{segs[1]}".lower(), segs[3])
