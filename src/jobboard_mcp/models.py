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


class Pay(BaseModel):
    """Result of pay normalization. min/max are annual USD or null."""

    min: int | None = None
    max: int | None = None
    currency: str | None = None
    interval: str | None = None
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
    pay_min: int | None = Field(default=None, description="Annual USD; null if unknown or non-USD")
    pay_max: int | None = Field(default=None, description="Annual USD; null if unknown or non-USD")
    pay_currency: str | None = None
    pay_interval: str | None = None
    pay_raw: str | None = None
    pay_source: PaySource = PaySource.NONE
    url: str
    apply_url: str | None = None
    description_text: str | None = None
    flags: list[str] = Field(
        default_factory=list,
        description=(
            "Why a job was kept despite missing data: pay_unknown, date_unknown, remote_unknown"
        ),
    )

    def set_pay(self, pay: Pay) -> None:
        self.pay_min, self.pay_max = pay.min, pay.max
        self.pay_currency, self.pay_interval = pay.currency, pay.interval
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
