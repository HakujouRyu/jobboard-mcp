"""Adapter protocol shared by every ATS, plus helpers for building normalized jobs."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from jobboard_mcp.http import HttpClient
from jobboard_mcp.models import Ats, Job
from jobboard_mcp.normalize import fingerprint


@dataclass(frozen=True)
class BoardListing:
    jobs: list[Job]
    cache_hit: bool


@dataclass(frozen=True)
class PostingRef:
    ats: Ats
    token: str
    job_id: str


class Adapter(Protocol):
    ats: Ats

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
