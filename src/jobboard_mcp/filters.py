"""Pure sweep filtering, flagging, and sorting.

Filtering runs in two stages so the sweep can fetch Greenhouse pay details only for jobs that
survive the cheap checks: ``prefilter`` (ids, title, remote, date) and then ``passes_pay``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlsplit

from jobboard_mcp.models import Job, Remote

# "internship" is listed separately because "intern" matches whole words only.
DEFAULT_TITLE_EXCLUDE = ("intern", "internship", "new grad", "principal")
DEFAULT_REMOTE = (Remote.REMOTE_US, Remote.REMOTE_GLOBAL)

FLAG_PAY_UNKNOWN = "pay_unknown"
FLAG_DATE_UNKNOWN = "date_unknown"
FLAG_REMOTE_UNKNOWN = "remote_unknown"


def compile_terms(terms: Iterable[str]) -> list[re.Pattern[str]]:
    """Plain terms match whole words, case-insensitively, plus a plain plural.

    "intern" matches "Interns" but not "Internal"; "engineer" does not match "Engineering".

    Prefix a term with ``re:`` to use it as a raw regular expression.
    """
    patterns: list[re.Pattern[str]] = []
    for term in terms:
        t = term.strip()
        if not t:
            continue
        if t.startswith("re:"):
            patterns.append(re.compile(t[3:], re.I))
        else:
            body = r"\s+".join(re.escape(w) for w in t.split())
            patterns.append(re.compile(rf"(?<!\w){body}(?:e?s)?(?!\w)", re.I))
    return patterns


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}"


@dataclass
class SweepFilters:
    today: date
    title_include: Sequence[str] = ()
    title_exclude: Sequence[str] = DEFAULT_TITLE_EXCLUDE
    remote: Sequence[Remote] = DEFAULT_REMOTE
    include_unknown_remote: bool = True
    min_pay: int | None = None
    posted_within_days: int | None = None
    exclude_ids: Sequence[str] = ()
    exclude_fingerprints: Sequence[str] = ()
    exclude_urls: Sequence[str] = ()
    _include: list[re.Pattern[str]] = field(init=False, repr=False)
    _exclude: list[re.Pattern[str]] = field(init=False, repr=False)
    _urls: set[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._include = compile_terms(self.title_include)
        self._exclude = compile_terms(self.title_exclude)
        self._urls = {normalize_url(u) for u in self.exclude_urls}

    @property
    def cutoff(self) -> date | None:
        if self.posted_within_days is None:
            return None
        return self.today - timedelta(days=self.posted_within_days)


def _title_ok(title: str, f: SweepFilters) -> bool:
    if f._include and not any(p.search(title) for p in f._include):
        return False
    return not any(p.search(title) for p in f._exclude)


def _excluded(job: Job, f: SweepFilters) -> bool:
    if job.id in f.exclude_ids or job.fingerprint in f.exclude_fingerprints:
        return True
    urls = {normalize_url(u) for u in (job.url, job.apply_url) if u}
    return bool(urls & f._urls)


def prefilter(job: Job, f: SweepFilters) -> bool:
    """Every check except pay. Unknown remote status and unknown dates are kept."""
    if _excluded(job, f) or not _title_ok(job.title, f):
        return False
    if job.remote is Remote.UNKNOWN:
        if not f.include_unknown_remote:
            return False
    elif job.remote not in f.remote:
        return False
    cutoff = f.cutoff
    return cutoff is None or job.posted_at is None or job.posted_at >= cutoff


def passes_pay(job: Job, f: SweepFilters) -> bool:
    """Drop jobs whose posted maximum is below ``min_pay``. Unknown pay is kept."""
    return f.min_pay is None or job.pay_max is None or job.pay_max >= f.min_pay


def flag(job: Job, f: SweepFilters) -> Job:
    """Record which active filters a kept job could not be checked against."""
    flags: list[str] = []
    if f.min_pay is not None and job.pay_max is None:
        flags.append(FLAG_PAY_UNKNOWN)
    if f.posted_within_days is not None and job.posted_at is None:
        flags.append(FLAG_DATE_UNKNOWN)
    if job.remote is Remote.UNKNOWN:
        flags.append(FLAG_REMOTE_UNKNOWN)
    return job.model_copy(update={"flags": flags})


def sort_jobs(jobs: Iterable[Job]) -> list[Job]:
    """Newest first, unknown dates last; ties broken deterministically."""
    return sorted(
        jobs,
        key=lambda j: (
            j.posted_at is None,
            -(j.posted_at.toordinal() if j.posted_at else 0),
            j.company.lower(),
            j.title.lower(),
            j.id,
        ),
    )


def apply_filters(jobs: Iterable[Job], f: SweepFilters) -> list[Job]:
    """Single-stage convenience: prefilter, pay check, flags, dedupe by id, and sort."""
    seen: set[str] = set()
    kept: list[Job] = []
    for job in jobs:
        if job.id in seen or not (prefilter(job, f) and passes_pay(job, f)):
            continue
        seen.add(job.id)
        kept.append(flag(job, f))
    return sort_jobs(kept)
