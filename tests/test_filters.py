import re
from datetime import date
from pathlib import Path

import pytest

from jobboard_mcp.filters import SweepFilters, apply_filters, compile_terms, sort_jobs
from jobboard_mcp.models import (
    FLAG_DATE_UNKNOWN,
    FLAG_PAY_UNKNOWN,
    FLAG_REMOTE_UNKNOWN,
    Ats,
    Job,
    PayAmount,
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
        "pay_currency": "USD",
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


# Keep in step with "Useful terms" in docs/normalization.md. Each term, then titles it must
# match, then titles it must not (the gotchas the table warns about).
USEFUL_TERMS: list[tuple[str, list[str], list[str]]] = [
    ("intern", ["Software Engineering Intern", "Interns - Summer 2027"], ["Internship"]),
    ("internship", ["Summer Internship 2027"], ["Internal Tools Engineer"]),
    ("new grad", ["Software Engineer, New Grad"], []),
    ("graduate", ["Graduate Software Engineer"], []),
    ("entry level", ["Entry-Level Data Analyst", "Entry Level Engineer"], []),
    ("junior", ["Junior Developer"], []),
    ("jr", ["Jr. Developer", "Developer (Jr)"], []),
    ("apprentice", ["Software Apprentice"], []),
    ("co-op", ["Co-op Student, Engineering", "Co op Student"], ["Coop"]),
    ("senior", ["Senior Backend Engineer"], []),
    ("sr", ["Sr. Backend Engineer", "Sr Backend Engineer"], ["Srinivasan"]),
    ("staff", ["Staff Engineer"], ["Staffing Coordinator"]),
    ("principal", ["Principal Engineer"], []),
    ("distinguished", ["Distinguished Engineer"], []),
    ("lead", ["Tech Lead", "Lead Generation Specialist"], ["Leadership Coach"]),
    ("manager", ["Engineering Manager", "Product Manager"], ["Management Consultant"]),
    ("director", ["Director of Engineering"], []),
    ("head of", ["Head of Platform"], []),
    ("vp", ["VP, Engineering", "VP of Product"], []),
    ("vice president", ["Vice President, Sales"], []),
    ("chief", ["Chief Technology Officer", "Chief of Staff"], []),
    ("engineer", ["Backend Engineer", "Forward Deployed Engineers"], ["Engineering Manager"]),
    ("engineering", ["Engineering Manager"], ["Backend Engineer"]),
    ("developer", ["iOS Developer"], ["Development Manager"]),
    ("programmer", ["Gameplay Programmer"], []),
    ("swe", ["SWE II"], ["Swedish Speaking Support"]),
    ("frontend", ["Frontend Engineer"], ["Front-End Engineer"]),
    ("front end", ["Front-End Engineer", "Front End Engineer"], ["Frontend Engineer"]),
    ("backend", ["Backend Engineer"], ["Back-End Engineer"]),
    ("back end", ["Back-End Engineer", "Back End Engineer"], ["Backend Engineer"]),
    ("full stack", ["Full-Stack Engineer", "Full Stack Engineer"], ["Fullstack Engineer"]),
    ("fullstack", ["Fullstack Engineer"], ["Full-Stack Engineer"]),
    ("sre", ["SRE", "Senior SREs"], ["Presales Engineer"]),
    ("site reliability", ["Site Reliability Engineer"], []),
    ("devops", ["DevOps Engineer"], []),
    ("platform", ["Platform Engineer"], []),
    ("infrastructure", ["Infrastructure Engineer"], []),
    ("data engineer", ["Senior Data Engineer"], ["Data Engineering Manager"]),
    ("data scientist", ["Data Scientist, Growth"], []),
    ("machine learning", ["Machine Learning Engineer"], []),
    ("ml", ["ML Engineer", "ML/AI Engineer"], ["HTML Developer"]),
    ("ai", ["AI Engineer", "Applied AI Researcher"], ["Maintenance Technician"]),
    ("product manager", ["Senior Product Manager"], ["Product Marketing Manager"]),
    ("designer", ["Product Designer"], ["Design Engineer"]),
    ("ux", ["UX Researcher", "UX/UI Designer"], ["Luxury Retail Associate"]),
    ("iii", ["Software Engineer III"], ["Software Engineer II"]),
]


@pytest.mark.parametrize(("term", "hits", "misses"), USEFUL_TERMS)
def test_useful_terms(term: str, hits: list[str], misses: list[str]) -> None:
    (pattern,) = compile_terms([term])
    for title in hits:
        assert pattern.search(title), f"{term!r} should match {title!r}"
    for title in misses:
        assert not pattern.search(title), f"{term!r} should not match {title!r}"


def test_useful_terms_table_lists_the_tested_terms() -> None:
    doc = (Path(__file__).parents[1] / "docs" / "normalization.md").read_text(encoding="utf-8")
    table = doc.split("#### Useful terms", 1)[1].split("\n\n|", 1)[1].split("\n\n", 1)[0]
    documented = {
        t for row in table.splitlines()[2:] for t in re.findall(r"`([^`]+)`", row.split("|")[2])
    }
    assert documented == {term for term, _, _ in USEFUL_TERMS} - {"iii"}


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


def test_min_pay_compares_only_the_same_currency() -> None:
    cad_too = [PayAmount(currency="CAD", min=130_000, max=150_000, interval="year")]
    jobs = [
        make_job(1, pay_max=90_000, pay_currency="EUR"),
        make_job(2, pay_max=60_000, pay_currency="EUR"),
        make_job(3, pay_max=200_000, pay_currency="USD", pay_other_currencies=cad_too),
        make_job(4, pay_max=120_000, pay_currency="USD"),  # nothing in EUR: kept
    ]
    eur = SweepFilters(today=TODAY, min_pay=80_000, min_pay_currency="EUR")
    assert sorted(j.native_id for j in apply_filters(jobs, eur)) == ["1", "3", "4"]
    # The CAD tier is found even though USD is the primary currency.
    cad = SweepFilters(today=TODAY, min_pay=160_000, min_pay_currency="CAD")
    assert sorted(j.native_id for j in apply_filters(jobs, cad)) == ["1", "2", "4"]
    # Default is USD, so EUR-only jobs are kept rather than compared.
    usd = SweepFilters(today=TODAY, min_pay=150_000)
    assert sorted(j.native_id for j in apply_filters(jobs, usd)) == ["1", "2", "3"]


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
        # Pay in another currency is known pay, not missing data.
        ({"pay_max": 90_000, "pay_currency": "EUR"}, []),
        ({"pay_max": None, "pay_currency": "EUR"}, [FLAG_PAY_UNKNOWN]),
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
