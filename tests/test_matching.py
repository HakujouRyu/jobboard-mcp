import pytest

from jobboard_mcp.matching import (
    MIN_SCORE,
    location_score,
    normalize_title,
    rank_postings,
    title_score,
)
from jobboard_mcp.models import Ats, Job


def job(n: int, title: str, locations: list[str] | None = None) -> Job:
    return Job(
        id=f"ashby:co:{n}",
        fingerprint=str(n),
        company="Co",
        ats=Ats.ASHBY,
        board_token="co",
        native_id=str(n),
        title=title,
        locations=locations or [],
        url=f"https://jobs.ashbyhq.com/co/{n}",
        description_text="long text",
    )


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        (
            "Sr. Software Engineer - Observability (Remote)",
            "senior software engineer observability",
        ),
        ("Senior SWE, Platform", "senior software engineer platform"),
        ("Software Engineer II", "software engineer 2"),
        ("Staff SRE (US)", "staff site reliability engineer"),
        ("Front End Engineer", "frontend engineer"),
    ],
)
def test_normalize_title(raw: str, normalized: str) -> None:
    assert normalize_title(raw) == normalized


@pytest.mark.parametrize(
    ("query", "title"),
    [
        (
            "Sr. Software Engineer - Observability (Remote)",
            "Senior Software Engineer, Observability",
        ),
        ("Senior SWE, Observability", "Senior Software Engineer, Observability"),
        ("Software Engineer II, Cloud Enablement", "Software Engineer 2 - Cloud Enablement"),
        ("Senior SRE", "Senior Site Reliability Engineer"),
    ],
)
def test_equivalent_titles_score_100(query: str, title: str) -> None:
    assert title_score(query, title) == 100.0


def test_partial_query_beats_wrong_seniority_and_wrong_role() -> None:
    q = "Senior Product Manager"
    exact_ish = title_score(q, "Senior Product Manager, Cloud")
    wrong_role = title_score(q, "Senior Product Marketing Manager")
    wrong_level = title_score(q, "Principal Product Manager")
    assert exact_ish > 85
    assert exact_ish > wrong_role and exact_ish > wrong_level
    assert title_score("Senior Backend Engineer", "Account Executive - France") < MIN_SCORE


def test_location_score() -> None:
    assert location_score("Remote US", ["Remote, US"]) == 100.0
    assert location_score("Seattle", ["Remote, United States", "Seattle, WA"]) == 100.0
    assert location_score("London", []) == 0.0


def test_rank_postings_orders_limits_and_strips_descriptions() -> None:
    jobs = [
        job(1, "Senior Software Engineer, Observability", ["United States (Remote)"]),
        job(2, "Senior Software Engineer, Cloud Growth", ["United States (Remote)"]),
        job(3, "Account Executive"),
        job(4, "Senior Software Engineer, Observability", ["London, England"]),
        *(job(10 + i, f"Senior Software Engineer {i}") for i in range(6)),
    ]
    got = rank_postings(jobs, "Sr Software Engineer - Observability", location="United States")
    assert len(got) == 5
    assert [m.job.native_id for m in got[:2]] == ["1", "4"]  # location breaks the tie
    assert got[0].title_score == 100.0 and got[0].location_score == 100.0
    assert all(m.job.description_text is None for m in got)
    assert "3" not in {m.job.native_id for m in got}


def test_rank_postings_no_location_and_no_matches() -> None:
    got = rank_postings([job(1, "Staff Data Scientist")], "Senior Backend Engineer")
    assert got == []
    (only,) = rank_postings([job(1, "Backend Engineer")], "backend engineer")
    assert only.location_score is None and only.score == 100.0
