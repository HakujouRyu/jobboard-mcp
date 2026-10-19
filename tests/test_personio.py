"""Personio adapter against recorded XML feeds (Vivid, thermondo, ottonova)."""

import time
from datetime import date

import httpx
import pytest
import respx

from jobboard_mcp.adapters.personio import PersonioAdapter
from jobboard_mcp.http import HttpClient, NotFound
from jobboard_mcp.models import Ats, EmploymentType, Job, PaySource, Remote, Source
from jobboard_mcp.resolve import _BOARD_PATTERNS, find_board_refs

from .conftest import fixture_text

PE = PersonioAdapter()


def mock_feed(token: str) -> respx.Route:
    return respx.get(PE.list_url(token)).mock(
        return_value=httpx.Response(200, text=fixture_text(f"personio_{token}.xml"))
    )


async def jobs(http: HttpClient, token: str) -> dict[str, Job]:
    mock_feed(token)
    return {j.native_id: j for j in (await PE.list_jobs(http, token, token)).jobs}


@respx.mock
async def test_list_builds_jobs(http: HttpClient) -> None:
    by_id = await jobs(http, "vivid")
    assert len(by_id) == 7
    job = by_id["2452785"]
    assert job.id == "personio:vivid:2452785"
    assert (job.ats, job.source) == (Ats.PERSONIO, Source.DOCUMENTED_API)
    assert job.title == "AML Process Owner"
    assert job.locations == ["Limassol"]
    assert job.team == "Fincrime Prevention Operations (2nd line)"
    assert (job.posted_at, job.date_source) == (date(2025, 12, 5), "createdAt")
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.url == "https://vivid.jobs.personio.de/job/2452785"
    assert PE.parse_url(job.url) is not None
    assert job.description_text and job.description_text.startswith("About The Role\n")
    assert "<span" not in job.description_text
    assert not PE.needs_detail(job)


@respx.mock
async def test_structured_pay(http: HttpClient) -> None:
    vivid = await jobs(http, "vivid")
    aml = vivid["2452785"]
    assert (aml.pay_min, aml.pay_max, aml.pay_currency) == (30000, 40000, "EUR")
    assert (aml.pay_posted_interval, aml.pay_source) == ("year", PaySource.STRUCTURED)
    assert aml.pay_raw and "currencyCode=EUR" in aml.pay_raw
    floor_only = vivid["2707648"]  # a minimum and no maximum
    assert (floor_only.pay_min, floor_only.pay_max) == (120000, None)
    assert vivid["2781380"].pay_source is PaySource.NONE
    thermondo = await jobs(http, "thermondo")
    monthly = thermondo["254619"]  # 3,700 to 4,400 a month
    assert (monthly.pay_min, monthly.pay_max, monthly.pay_posted_interval) == (
        44400,
        52800,
        "month",
    )
    assert thermondo["2841855"].pay_posted_interval == "hour"


@pytest.mark.parametrize(
    ("token", "job_id", "remote"),
    [
        ("vivid", "2452785", Remote.ONSITE),  # "Limassol"
        ("vivid", "2529293", Remote.REMOTE_OTHER),  # "... (Remote)", Philippines
        ("vivid", "2818897", Remote.REMOTE_OTHER),  # "Remote" among non-US offices
        ("vivid", "2781380", Remote.REMOTE_OTHER),  # "Austria", no city
        ("thermondo", "2813871", Remote.REMOTE_OTHER),  # "Berlin (Zentrale)" and "Remote"
    ],
)
@respx.mock
async def test_remote_from_offices(
    http: HttpClient, token: str, job_id: str, remote: Remote
) -> None:
    assert (await jobs(http, token))[job_id].remote is remote


@respx.mock
async def test_additional_offices_are_locations(http: HttpClient) -> None:
    job = (await jobs(http, "vivid"))["2818897"]
    assert job.locations == ["Remote", "Almaty", "Limassol", "Barcelona", "Serbia"]


@pytest.mark.parametrize(
    ("token", "job_id", "kind"),
    [
        ("vivid", "2814908", EmploymentType.CONTRACT),  # fixed_term
        ("thermondo", "2841855", EmploymentType.INTERN),  # working_student
        ("thermondo", "2119725", EmploymentType.INTERN),  # intern
        ("ottonova", "2573573", EmploymentType.FULL_TIME),  # permanent, full-or-part-time
        ("ottonova", "2821399", EmploymentType.INTERN),
    ],
)
@respx.mock
async def test_employment_type(
    http: HttpClient, token: str, job_id: str, kind: EmploymentType
) -> None:
    assert (await jobs(http, token))[job_id].employment_type is kind


@respx.mock
async def test_get_job_and_check_live_use_the_feed(http: HttpClient) -> None:
    mock_feed("vivid")
    job = await PE.get_job(http, "Vivid", "vivid", "2818897")
    assert job.title == "Security Engineer (Hybrid or Remote)"
    assert (await PE.check_live(http, "vivid", "2818897")).live
    gone = await PE.check_live(http, "vivid", "1")
    assert not gone.live and "no longer listed" in gone.reason
    with pytest.raises(NotFound):
        await PE.get_job(http, "Vivid", "vivid", "1")


@respx.mock
async def test_empty_and_unknown_boards(http: HttpClient) -> None:
    respx.get(PE.list_url("circula")).mock(
        return_value=httpx.Response(200, text=fixture_text("personio_empty.xml"))
    )
    assert (await PE.list_jobs(http, "Circula", "circula")).jobs == []
    # An unknown company gets an HTML error page with a 404.
    respx.get(PE.list_url("nosuch")).mock(
        return_value=httpx.Response(404, text=fixture_text("personio_404.html"))
    )
    with pytest.raises(NotFound):
        await PE.list_jobs(http, "Nosuch", "nosuch")
    assert not (await PE.check_live(http, "nosuch", "1")).live


@respx.mock
async def test_probe_reports_jobs_and_hiring_entities(http: HttpClient) -> None:
    mock_feed("vivid")
    probe = await PE.probe(http, "vivid")
    assert probe.jobs == 7
    assert "Vivid Money GmbH" in probe.names


@pytest.mark.parametrize(
    "token", ["evil.com/x#", "a.b", "", "-acme", "acme-", "x" * 64, "acme:8080"]
)
def test_tokens_must_be_one_hostname_label(token: str) -> None:
    # The token becomes the hostname, so anything else could send the request elsewhere.
    with pytest.raises(ValueError, match="Personio company name"):
        PE.list_url(token)


def test_token_case_is_folded() -> None:
    assert PE.list_url(" Vivid ") == "https://vivid.jobs.personio.de/xml?language=en"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://vivid.jobs.personio.de/job/2452785", ("vivid", "2452785")),
        ("https://Vivid.jobs.personio.com/job/2452785?language=en", ("vivid", "2452785")),
        ("https://vivid.jobs.personio.de/", None),
        ("https://vivid.jobs.personio.de/job/abc", None),
        ("https://www.personio.de/job/2452785", None),
        ("https://a.b.jobs.personio.de/job/1", None),
    ],
)
def test_parse_url(url: str, expected: tuple[str, str] | None) -> None:
    ref = PE.parse_url(url)
    if expected is None:
        assert ref is None
    else:
        assert ref is not None and (ref.ats, ref.token, ref.job_id) == (Ats.PERSONIO, *expected)


def test_personio_links_found_on_pages() -> None:
    html = (
        '<a href="https://vivid.jobs.personio.de/">Jobs</a>'
        '<iframe src="https://acme-gmbh.jobs.personio.com/?display=en"></iframe>'
        '<a href="https://www.personio.de/">HR software</a>'
    )
    refs = find_board_refs(html)
    assert {(Ats.PERSONIO, "vivid"), (Ats.PERSONIO, "acme-gmbh")} <= set(refs)
    assert not any(t in ("www", "personio") for _, t in refs)


def test_board_pattern_is_linear_on_hostile_input() -> None:
    # Ground rule 9: long runs of label characters, with and without a near miss.
    pattern = next(p for ats, p in _BOARD_PATTERNS if ats is Ats.PERSONIO)
    for html in ("a" * 200_000, "a-" * 100_000, ("a" * 70 + ".jobs.personio.") * 3000):
        started = time.perf_counter()
        list(pattern.finditer(html))
        assert time.perf_counter() - started < 1.0
