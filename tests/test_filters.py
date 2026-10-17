from datetime import date

import pytest

from jobboard_mcp.filters import SweepFilters, apply_filters, compile_terms, sort_jobs
from jobboard_mcp.models import (
    FLAG_DATE_UNKNOWN,
    FLAG_PAY_NOT_USD,
    FLAG_PAY_UNKNOWN,
    FLAG_REMOTE_UNKNOWN,
    Ats,
    Job,
    Remote,
)

TODAY = date(2026, 10, 15)


def make_job(n: int = 1, **kw: object) -> Job:
    data: dict[str, object] = {
        "id": f"ashby:acme:{n}",
        "fingerprint": f"fp{n}",
        "company": "Acme",
        "ats": Ats.ASHBY,
        "board_token": "acme",
        "native_id": str(n),
        "title": "Senior Software Engineer",
        "remote": Remote.REMOTE_US,
        "posted_at": date(2026, 10, 10),
        "pay_max": 200_000,
        "url": f"https://jobs.ashbyhq.com/acme/{n}",
    }
    data.update(kw)
    return Job.model_validate(data)


@pytest.mark.parametrize(
    ("term", "title", "hit"),
    [
        ("intern", "Software Engineering Intern", True),
        ("intern", "Internal Tools Engineer", False),
        ("intern", "International Payments Lead", False),
        ("internship", "Summer Internship 2027", True),
        ("new grad", "Software Engineer, New  Grad", True),
        ("full stack", "Full-Stack Engineer", True),
        ("full-stack", "Full Stack Engineer", True),
        ("full stack", "Fullstack Engineer", False),  # no separator: pass "fullstack" too
        ("entry level", "Software Engineer (Entry-Level)", True),
        ("e-commerce", "E-Commerce Analyst", True),
        ("principal", "Principal Engineer", True),
        ("sre", "SRE II", True),
        ("sre", "Presales Engineer", False),
        ("engineer", "Engineer", True),
        ("engineer", "Forward Deployed Engineers", True),
        ("engineer", "Engineering Manager", False),
        ("re:engineer(ing)?", "Engineering Manager", False),  # literal text, not a regex
        ("c++", "C++ Developer", True),
    ],
)
def test_compile_terms(term: str, title: str, hit: bool) -> None:
    (pattern,) = compile_terms([term])
    assert bool(pattern.search(title)) is hit


def test_blank_and_separator_only_terms_are_ignored() -> None:
    assert compile_terms(["", "  ", "-", " - "]) == []


def test_title_include_and_exclude() -> None:
    jobs = [
        make_job(1, title="Backend Engineer"),
        make_job(2, title="Principal Engineer"),
        make_job(3, title="Account Executive"),
        make_job(4, title="Internal Tools Engineer"),
    ]
    got = apply_filters(
        jobs, SweepFilters(today=TODAY, title_include=["engineer"], title_exclude=["principal"])
    )
    assert [j.native_id for j in got] == ["1", "4"]


def test_no_filters_keep_everything() -> None:
    jobs = [
        make_job(1, title="Principal Engineer", remote=Remote.ONSITE),
        make_job(2, title="Software Engineering Intern", remote=Remote.REMOTE_OTHER),
        make_job(3, title="Account Executive", remote=Remote.UNKNOWN),
    ]
    assert len(apply_filters(jobs, SweepFilters(today=TODAY))) == 3


def test_remote_filter_and_unknown_handling() -> None:
    jobs = [
        make_job(1, remote=Remote.REMOTE_US),
        make_job(2, remote=Remote.REMOTE_OTHER),
        make_job(3, remote=Remote.HYBRID),
        make_job(4, remote=Remote.UNKNOWN),
        make_job(5, remote=Remote.REMOTE_GLOBAL),
    ]
    remote = [Remote.REMOTE_US, Remote.REMOTE_GLOBAL]
    got = apply_filters(jobs, SweepFilters(today=TODAY, remote=remote))
    assert sorted(j.native_id for j in got) == ["1", "4", "5"]
    unknown = next(j for j in got if j.native_id == "4")
    assert FLAG_REMOTE_UNKNOWN in unknown.flags

    strict = apply_filters(
        jobs, SweepFilters(today=TODAY, remote=remote, include_unknown_remote=False)
    )
    assert sorted(j.native_id for j in strict) == ["1", "5"]

    no_unknown = apply_filters(jobs, SweepFilters(today=TODAY, include_unknown_remote=False))
    assert sorted(j.native_id for j in no_unknown) == ["1", "2", "3", "5"]

    us_only = apply_filters(jobs, SweepFilters(today=TODAY, remote=[Remote.REMOTE_US]))
    assert sorted(j.native_id for j in us_only) == ["1", "4"]


def test_min_pay_drops_low_max_and_flags_unknown() -> None:
    jobs = [
        make_job(1, pay_max=90_000),
        make_job(2, pay_max=100_000),
        make_job(3, pay_max=None),
    ]
    got = apply_filters(jobs, SweepFilters(today=TODAY, min_pay=100_000))
    assert sorted(j.native_id for j in got) == ["2", "3"]
    assert next(j for j in got if j.native_id == "3").flags == [FLAG_PAY_UNKNOWN]
    assert next(j for j in got if j.native_id == "2").flags == []


def test_posted_within_days() -> None:
    jobs = [
        make_job(1, posted_at=date(2026, 9, 24)),  # exactly 21 days
        make_job(2, posted_at=date(2026, 9, 23)),
        make_job(3, posted_at=None),
    ]
    got = apply_filters(jobs, SweepFilters(today=TODAY, posted_within_days=21))
    assert [j.native_id for j in got] == ["1", "3"]
    assert got[1].flags == [FLAG_DATE_UNKNOWN]


def test_flags_do_not_depend_on_filters() -> None:
    # Missing pay and date are flagged even when the sweep doesn't filter on them
    (job,) = apply_filters([make_job(1, pay_max=None, posted_at=None)], SweepFilters(today=TODAY))
    assert job.flags == [FLAG_PAY_UNKNOWN, FLAG_DATE_UNKNOWN]


@pytest.mark.parametrize(
    ("pay", "expected"),
    [
        ({"pay_max": 200_000}, []),
        ({"pay_max": None, "pay_min": 120_000}, []),  # "$120,000+" is known pay
        ({"pay_max": None}, [FLAG_PAY_UNKNOWN]),
        # Mozilla posts EUR and CAD ranges: pay is known, just not comparable to min_pay
        ({"pay_max": None, "pay_currency": "EUR"}, [FLAG_PAY_NOT_USD]),
        ({"pay_max": 150_000, "pay_currency": "USD"}, []),
    ],
)
def test_pay_flags(pay: dict[str, object], expected: list[str]) -> None:
    assert make_job(1, **pay).flags == expected


def test_flags_follow_pay_filled_in_later() -> None:
    # Greenhouse list jobs lack pay until the sweep fetches the detail page
    job = make_job(1, pay_max=None)
    assert job.flags == [FLAG_PAY_UNKNOWN]
    assert job.model_copy(update={"pay_max": 180_000}).flags == []


def test_remote_unknown_flag() -> None:
    assert make_job(1, remote=Remote.UNKNOWN).flags == [FLAG_REMOTE_UNKNOWN]


def test_exclusions() -> None:
    jobs = [make_job(1), make_job(2), make_job(3), make_job(4)]
    f = SweepFilters(
        today=TODAY,
        exclude_ids=["ashby:acme:1"],
        exclude_fingerprints=["fp2"],
        exclude_urls=["https://JOBS.ashbyhq.com/acme/3/?utm_source=x"],
    )
    assert [j.native_id for j in apply_filters(jobs, f)] == ["4"]


def test_sort_newest_first_unknown_last_deterministic() -> None:
    jobs = [
        make_job(1, posted_at=None),
        make_job(2, posted_at=date(2026, 10, 1), title="B"),
        make_job(3, posted_at=date(2026, 10, 14)),
        make_job(4, posted_at=date(2026, 10, 1), title="A"),
    ]
    assert [j.native_id for j in sort_jobs(jobs)] == ["3", "4", "2", "1"]


def test_dedupes_by_id() -> None:
    assert len(apply_filters([make_job(1), make_job(1)], SweepFilters(today=TODAY))) == 1
