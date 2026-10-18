"""Adapter protocol shared by every ATS, plus helpers for building normalized jobs."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from jobboard_mcp.http import HttpClient
from jobboard_mcp.models import Ats, Job, Source
from jobboard_mcp.normalize import fingerprint, https_url

# The fields a Greenhouse detail call adds to a listed job.
PAY_FIELDS = frozenset(
    {"pay_min", "pay_max", "pay_currency", "pay_posted_interval", "pay_raw", "pay_source"}
)


@dataclass(frozen=True)
class BoardListing:
    jobs: list[Job]
    cache_hit: bool
    note: str | None = None  # anything the caller should know, e.g. a search that hit a cap


@dataclass(frozen=True)
class BoardProbe:
    jobs: int
    names: set[str]  # company names the board reports about itself (Greenhouse only)


@dataclass(frozen=True)
class LiveCheck:
    live: bool
    reason: str
    cache_hit: bool


@dataclass(frozen=True)
class PostingRef:
    ats: Ats
    token: str
    job_id: str


class Adapter(Protocol):
    ats: Ats
    source: Source  # every job it builds carries this; pass it to build_job when not tier 1
    guess_tokens: bool  # resolve_board probes guessed tokens here (False when probing is costly)
    detail_fields: frozenset[str]  # Job fields that get_job fills in for a swept job
    max_details: int | None  # most detail calls per board in one sweep; None for no cap

    def list_url(self, token: str) -> str:
        """The board's public list endpoint."""
        ...

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
        """The board's listed jobs, normalized. ``search`` holds the sweep's title terms: an
        ATS that can't list a large board in full searches for them instead (the caller still
        filters). ``today`` lets an ATS that shows relative dates ("Posted 3 Days Ago") turn
        them into dates. Both are hints that other adapters ignore."""
        ...

    async def probe(self, http: HttpClient, token: str, *, refresh: bool = False) -> BoardProbe:
        """Cheaply check that a board exists. Raises FetchError if it doesn't answer."""
        ...

    async def get_job(
        self, http: HttpClient, company: str, token: str, job_id: str, *, refresh: bool = False
    ) -> Job:
        """One job with the best available pay data. Raises NotFound if it is gone."""
        ...

    async def check_live(
        self, http: HttpClient, token: str, job_id: str, *, refresh: bool = False
    ) -> LiveCheck:
        """Whether the posting is still up, from the ATS API (never a page fetch)."""
        ...

    def needs_detail(self, job: Job) -> bool:
        """True when ``get_job`` can fill in ``detail_fields`` that the listing lacks."""
        ...

    def parse_url(self, url: str) -> PostingRef | None:
        """Recognize a posting URL on this ATS."""
        ...


def build_job(
    *,
    ats: Ats,
    token: str,
    native_id: str | int,
    company: str,
    title: str,
    locations: list[str],
    **fields: object,
) -> Job:
    nid = str(native_id)
    title = " ".join(title.split())
    for key in ("url", "apply_url"):
        value = fields.get(key)
        if isinstance(value, str):
            fields[key] = https_url(value)
    return Job.model_validate(
        {
            "id": f"{ats.value}:{token}:{nid}",
            "fingerprint": fingerprint(company, title, locations),
            "company": company,
            "ats": ats,
            "board_token": token,
            "native_id": nid,
            "title": title,
            "locations": locations,
            **fields,
        }
    )


def dedupe(values: Iterable[str]) -> list[str]:
    """Strip, drop empties, and remove duplicates while keeping order."""
    return list(dict.fromkeys(v.strip() for v in values if v and v.strip()))


class ListingMemo:
    """Reuse normalized jobs while the HTTP cache keeps serving the same response object.

    Warm sweeps would otherwise re-run HTML and pay parsing on every cached board. Jobs are
    shared between calls, so callers must treat them as immutable (use ``model_copy``).
    """

    def __init__(self) -> None:
        self._memo: dict[tuple[str, str], tuple[Any, list[Job]]] = {}

    def jobs(self, company: str, url: str, data: Any, parse: Callable[[], list[Job]]) -> list[Job]:
        hit = self._memo.get((company, url))
        if hit is not None and hit[0] is data:
            return hit[1]
        jobs = parse()
        self._memo[(company, url)] = (data, jobs)
        return jobs
