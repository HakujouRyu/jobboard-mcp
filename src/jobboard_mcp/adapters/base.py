"""Adapter protocol shared by every ATS, plus helpers for building normalized jobs."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from jobboard_mcp.http import HttpClient
from jobboard_mcp.models import FLAG_REMOTE_UNKNOWN, Ats, Job, Remote
from jobboard_mcp.normalize import fingerprint, https_url


@dataclass(frozen=True)
class BoardListing:
    jobs: list[Job]
    cache_hit: bool


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

    def list_url(self, token: str) -> str:
        """The board's public list endpoint."""
        ...

    async def list_jobs(
        self, http: HttpClient, company: str, token: str, *, refresh: bool = False
    ) -> BoardListing:
        """Every listed job on the board, normalized, with ``description_text`` filled in."""
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

    def needs_detail_for_pay(self, job: Job) -> bool:
        """True when ``get_job`` can find pay that the list endpoint lacks."""
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
    if fields.get("remote") is Remote.UNKNOWN:
        fields.setdefault("flags", [FLAG_REMOTE_UNKNOWN])
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
