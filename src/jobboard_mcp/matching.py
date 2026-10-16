"""Fuzzy matching of a role seen elsewhere (LinkedIn, Indeed...) to postings on a company board.

Pure functions. Titles are normalized (Sr./Senior, SWE, roman-numeral levels, punctuation,
"(Remote)" noise) before rapidfuzz scoring, and a seniority mismatch costs points.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from rapidfuzz import fuzz

from jobboard_mcp.models import Job, PostingMatch
from jobboard_mcp.normalize import normalize_text

MIN_SCORE = 55.0
MAX_CANDIDATES = 5
SENIORITY_PENALTY = 15.0
ROLE_PENALTY = 15.0
LOCATION_WEIGHT = 0.15

_EXPAND = [
    (r"\bswe\b", "software engineer"),
    (r"\bsde\b", "software development engineer"),
    (r"\bsre\b", "site reliability engineer"),
    (r"\bml\b", "machine learning"),
    (r"\bpm\b", "product manager"),
    (r"\bem\b", "engineering manager"),
    (r"\bdevops\b", "dev ops"),
    (r"\bfront end\b", "frontend"),
    (r"\bback end\b", "backend"),
    (r"\bfull stack\b", "fullstack"),
    (r"\biii\b", "3"),
    (r"\bii\b", "2"),
    (r"\biv\b", "4"),
    (r"\bi\b", "1"),
]
# Location and arrangement words that boards and aggregators add to titles inconsistently.
_NOISE = re.compile(
    r"\b(remote|hybrid|onsite|on site|us|usa|united states|north america|emea|apac|"
    r"canada|uk|europe|anywhere|global|contract|full time|fulltime)\b"
)
_SENIORITY = {
    "intern", "junior", "associate", "mid", "senior", "staff", "principal", "lead",
    "distinguished", "head", "director", "vp",
}  # fmt: skip
# Words that change what the job is. "Product Manager" and "Product Marketing Manager" share
# every query word, so subset matching alone would call them near-identical.
_ROLE = {
    "engineer", "developer", "architect", "manager", "director", "designer", "scientist",
    "researcher", "analyst", "marketing", "sales", "account", "executive", "recruiter",
    "consultant", "specialist", "advocate", "writer", "administrator", "operations", "support",
    "success", "counsel", "accountant", "partner", "owner",
}  # fmt: skip


def normalize_title(title: str) -> str:
    s = normalize_text(title)
    for pattern, repl in _EXPAND:
        s = re.sub(pattern, repl, s)
    s = _NOISE.sub(" ", s)
    return " ".join(s.split())


def _seniority(normalized: str) -> set[str]:
    return set(normalized.split()) & _SENIORITY


def title_score(query: str, title: str) -> float:
    """0-100. Mixes order-insensitive similarity with subset matching, so a query that names
    only part of a title still scores well, then penalizes differing seniority or role."""
    q, t = normalize_title(query), normalize_title(title)
    if not q or not t:
        return 0.0
    score = 0.6 * fuzz.token_sort_ratio(q, t) + 0.4 * fuzz.token_set_ratio(q, t)
    sq, st = _seniority(q), _seniority(t)
    if sq and st and sq != st:
        score -= SENIORITY_PENALTY
    rq, rt = set(q.split()) & _ROLE, set(t.split()) & _ROLE
    if rq and rt and rq != rt:
        score -= ROLE_PENALTY
    return round(max(score, 0.0), 1)


def location_score(query: str, locations: Sequence[str]) -> float:
    q = normalize_text(query)
    if not q or not locations:
        return 0.0
    return round(max(fuzz.partial_ratio(q, normalize_text(loc)) for loc in locations), 1)


def rank_postings(
    jobs: Iterable[Job],
    title: str,
    location: str | None = None,
    *,
    limit: int = MAX_CANDIDATES,
    min_score: float = MIN_SCORE,
) -> list[PostingMatch]:
    matches: list[PostingMatch] = []
    for job in jobs:
        ts = title_score(title, job.title)
        ls = location_score(location, job.locations) if location else None
        score = ts if ls is None else (1 - LOCATION_WEIGHT) * ts + LOCATION_WEIGHT * ls
        if score >= min_score:
            matches.append(
                PostingMatch(
                    score=round(score, 1),
                    title_score=ts,
                    location_score=ls,
                    job=job.model_copy(update={"description_text": None}),
                )
            )
    matches.sort(key=lambda m: (-m.score, m.job.title.lower(), m.job.id))
    return matches[:limit]
