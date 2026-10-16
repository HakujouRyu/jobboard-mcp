"""Pydantic models for normalized jobs, the watchlist, and tool results."""

from __future__ import annotations

import json
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Ats(StrEnum):
    GREENHOUSE = "greenhouse"
    ASHBY = "ashby"
    LEVER = "lever"


class Remote(StrEnum):
    REMOTE_US = "remote_us"
    REMOTE_GLOBAL = "remote_global"
    REMOTE_OTHER = "remote_other"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERN = "intern"
    UNKNOWN = "unknown"


class PaySource(StrEnum):
    STRUCTURED = "structured"
    PARSED_FROM_TEXT = "parsed_from_text"
    NONE = "none"


class CompanyStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"  # board token returned 404
    NO_BOARD = "no_board"  # watchlist entry has no known ATS/token
    ERROR = "error"
    TIMEOUT = "timeout"


# Job.flags values. remote_unknown is set on every job whose remote status is unknown; the
# other two only by a sweep that filters on pay or date.
FLAG_PAY_UNKNOWN = "pay_unknown"
FLAG_DATE_UNKNOWN = "date_unknown"
FLAG_REMOTE_UNKNOWN = "remote_unknown"


class Pay(BaseModel):
    """Result of pay normalization. min/max are annual USD or null."""

    min: int | None = None
    max: int | None = None
    currency: str | None = None
    interval: str | None = None  # as posted: year / hour / month / week / mixed
    raw: str | None = None
    source: PaySource = PaySource.NONE


class Job(BaseModel):
    id: str = Field(description="{ats}:{token}:{native_id}")
    fingerprint: str = Field(description="Hash of normalized company + title + locations")
    company: str
    ats: Ats
    board_token: str
    native_id: str
    title: str
    team: str | None = None
    locations: list[str] = Field(default_factory=list)
    remote: Remote = Remote.UNKNOWN
    employment_type: EmploymentType | None = None
    posted_at: date | None = None
    updated_at: date | None = None
    date_source: str | None = None
    pay_min: int | None = Field(
        default=None,
        description="Annual USD (hourly pay x 2080, monthly x 12); null if unknown or non-USD",
    )
    pay_max: int | None = Field(
        default=None,
        description="Annual USD (hourly pay x 2080, monthly x 12); null if unknown or non-USD",
    )
    pay_currency: str | None = None
    pay_posted_interval: str | None = Field(
        default=None,
        description=(
            "How the pay was posted: year, hour, month, week, or mixed. "
            "pay_min and pay_max are always annualized."
        ),
    )
    pay_raw: str | None = None
    pay_source: PaySource = PaySource.NONE
    url: str
    apply_url: str | None = None
    description_text: str | None = None
    flags: list[str] = Field(
        default_factory=list,
        description=(
            "Missing data: remote_unknown on any job whose remote status is unknown; "
            "pay_unknown and date_unknown when a sweep filtered on pay or date"
        ),
    )

    def set_pay(self, pay: Pay) -> None:
        self.pay_min, self.pay_max = pay.min, pay.max
        self.pay_currency, self.pay_posted_interval = pay.currency, pay.interval
        self.pay_raw, self.pay_source = pay.raw, pay.source


class WatchlistEntry(BaseModel):
    company: str
    ats: Ats | None = None
    token: str | None = None
    careers_url: str | None = None
    domains: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    interview_note: str | None = None
    notes: str | None = None


class CompanyResult(BaseModel):
    company: str
    ats: Ats | None = None
    total: int = 0
    matched: int = 0
    status: CompanyStatus
    error: str | None = None


class SweepResult(BaseModel):
    jobs: list[Job]
    per_company: list[CompanyResult]
    fetched_at: datetime
    cache_hits: int = Field(description="Boards served from cache instead of refetched")
    flag_counts: dict[str, int] = Field(
        default_factory=dict, description="How many returned jobs carry each flag"
    )


def raw_json(value: Any) -> str:
    """Compact, stable JSON for pay_raw and similar audit fields."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- M2 results


class Confidence(StrEnum):
    HIGH = "high"  # careers page links to the board, or the board names the company
    MEDIUM = "medium"  # a guessed token returned a non-empty board
    LOW = "low"  # a guessed token exists but the board is empty


class BoardCandidate(BaseModel):
    ats: Ats
    token: str
    jobs: int
    confidence: Confidence
    evidence: list[str] = Field(default_factory=list)


class Resolution(BaseModel):
    ats: Ats | None = None
    token: str | None = None
    confidence: Confidence | None = None
    jobs: int | None = Field(default=None, description="Jobs on the board when checked")
    evidence: list[str] = Field(default_factory=list)
    alternatives: list[BoardCandidate] = Field(
        default_factory=list, description="Other boards that also answered"
    )
    pages_checked: list[str] = Field(default_factory=list)


class PostingMatch(BaseModel):
    score: float = Field(description="0-100; title similarity, adjusted for location if given")
    title_score: float
    location_score: float | None = None
    job: Job


class FindResult(BaseModel):
    company: str
    title: str
    board_found: bool
    ats: Ats | None = None
    token: str | None = None
    resolution: Resolution | None = Field(
        default=None, description="Present when the board was not on the watchlist"
    )
    candidates: list[PostingMatch] = Field(default_factory=list)


class LiveResult(BaseModel):
    live: bool
    checked_at: datetime
    reason: str
    ats: Ats
    token: str
    job_id: str
    from_cache: bool = Field(description="Answered from the cache rather than a new request")


class BoardCheck(BaseModel):
    status: CompanyStatus
    jobs: int = 0
    error: str | None = None


class WatchlistView(BaseModel):
    companies: list[WatchlistEntry]
    total: int
    readonly: bool


class WatchlistChange(BaseModel):
    entry: WatchlistEntry
    board_check: BoardCheck | None = Field(
        default=None, description="Live check of the board, when one is set"
    )
