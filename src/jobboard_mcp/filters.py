"""Pure sweep filtering and sorting.

Filtering runs in two stages so the sweep can fetch Greenhouse pay details only for jobs that
survive the cheap checks: ``prefilter`` (ids, title, remote, date) and then ``passes_pay``.

Every filter is off unless the caller sets it. There are no built-in preferences.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlsplit

from jobboard_mcp.models import Job, Remote


def compile_terms(terms: Iterable[str]) -> list[re.Pattern[str]]:
    """Plain terms match whole words, case-insensitively, plus a plain plural.

    "intern" matches "Interns" but not "Internal"; "engineer" does not match "Engineering".
    Words in a term may be separated by spaces or hyphens, in the term or the title, so
    "full stack" and "full-stack" both match "Full-Stack" and "Full Stack".

    Terms are always escaped. Raw regexes from the client aren't accepted, because a pattern
    like ``(a+)+$`` can backtrack for seconds and block the event loop.
    """
    patterns: list[re.Pattern[str]] = []
    for term in terms:
        words = [w for w in re.split(r"[\s-]+", term) if w]
        if words:
            body = r"[\s-]+".join(re.escape(w) for w in words)
            patterns.append(re.compile(rf"(?<!\w){body}(?:e?s)?(?!\w)", re.I))
    return patterns


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}"


@dataclass
class SweepFilters:
    today: date
    title_include: Sequence[str] = ()
    title_exclude: Sequence[str] = ()
    remote: Sequence[Remote] = ()  # empty keeps every status
    include_unknown_remote: bool = True
    min_pay: int | None = None
    min_pay_currency: str = "USD"
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
    elif f.remote and job.remote not in f.remote:
        return False
    cutoff = f.cutoff
    return cutoff is None or job.posted_at is None or job.posted_at >= cutoff


def passes_pay(job: Job, f: SweepFilters) -> bool:
    """Drop jobs whose maximum in ``min_pay_currency`` is below ``min_pay``. Jobs with no pay
    in that currency are kept: currencies are never converted, so they can't be compared."""
    if f.min_pay is None:
        return True
    top = job.pay_max_in(f.min_pay_currency)
    return top is None or top >= f.min_pay


def unverified(job: Job, f: SweepFilters) -> list[str]:
    """Filters the job was kept under without being checked against, because the data
    needed to check it is missing (or, for pay, posted only in other currencies).
    Named as the sweep_boards arguments are, in their order."""
    names: list[str] = []
    if f.remote and job.remote is Remote.UNKNOWN:
        names.append("remote")
    if f.min_pay is not None and job.pay_max_in(f.min_pay_currency) is None:
        names.append("min_pay")
    if f.cutoff is not None and job.posted_at is None:
        names.append("posted_within_days")
    return names


def mark_unverified(job: Job, f: SweepFilters) -> Job:
    """A copy of ``job`` with ``unverified_filters`` set for this search. Never changes
    ``job`` itself: listed jobs are shared through the adapters' memo."""
    return job.model_copy(update={"unverified_filters": unverified(job, f)})


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
    """Single-stage convenience: prefilter, pay check, dedupe by id, mark unverified filters,
    and sort."""
    seen: set[str] = set()
    kept: list[Job] = []
    for job in jobs:
        if job.id in seen or not (prefilter(job, f) and passes_pay(job, f)):
            continue
        seen.add(job.id)
        kept.append(mark_unverified(job, f))
    return sort_jobs(kept)
