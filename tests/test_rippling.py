"""Rippling adapter against a recorded job board API response (Rippling's own board)."""

import httpx
import pytest
import respx

from jobboard_mcp.adapters.rippling import MAX_PAGES, RipplingAdapter
from jobboard_mcp.http import HttpClient, NotFound
from jobboard_mcp.models import Ats, Job, Remote, Source
from jobboard_mcp.resolve import find_board_refs

from .conftest import fixture_json

RP = RipplingAdapter()
BOARD = "rippling"


def mock_board() -> respx.Route:
    return respx.get(RP.list_url(BOARD)).mock(
        return_value=httpx.Response(200, json=fixture_json("rippling_rippling.json"))
    )


async def board_jobs(http: HttpClient) -> dict[str, Job]:
    return {j.title: j for j in (await RP.list_jobs(http, "Rippling", BOARD)).jobs}


@respx.mock
async def test_list_builds_jobs(http: HttpClient) -> None:
    mock_board()
    listing = await RP.list_jobs(http, "Rippling", BOARD)
    assert len(listing.jobs) == 12 and listing.note is None
    job = next(j for j in listing.jobs if j.title == "Technical Support Specialist, Platform")
    assert job.id == f"rippling:rippling:{job.native_id}"
    assert (job.ats, job.source) == (Ats.RIPPLING, Source.DOCUMENTED_API)
    assert job.locations == ["Remote (United States)"]
    assert job.remote is Remote.REMOTE_US
    assert job.team == "Customer Support"
    assert job.url == f"https://ats.rippling.com/rippling/jobs/{job.native_id}"
    assert RP.parse_url(job.url) is not None
    # The list has no pay, date, description, or employment type, and there's no detail call.
    assert (job.posted_at, job.pay_min, job.description_text) == (None, None, None)
    assert {"pay_unknown", "date_unknown"} <= set(job.flags)
    assert job.employment_type is None and not RP.needs_detail(job)


@respx.mock
async def test_titles_are_trimmed(http: HttpClient) -> None:
    mock_board()
    jobs = await board_jobs(http)
    assert "Senior Engineering Manager - Compliance" in jobs  # " ... Compliance " on the board
    assert "Account Executive, Broker Channel (Dallas)" in jobs


@pytest.mark.parametrize(
    ("title", "remote"),
    [
        # Each location is ON_SITE, HYBRID, or REMOTE.
        ("Customer Support Specialist - Benefits Marketplace", Remote.REMOTE_US),  # 12 US states
        ("Senior Manager, Support Operations", Remote.REMOTE_US),  # NY and SF on-site, US remote
        ("Account Executive, Broker Channel (San Jose / South Bay)", Remote.REMOTE_US),  # cities
        ("HR Associate (PST/EST Hours)", Remote.REMOTE_OTHER),  # remote, Philippines
        ("Senior Solutions Consultant", Remote.REMOTE_OTHER),  # "Remote (Germany)"
        ("Technical Accounting Manager", Remote.HYBRID),
        ("Engineering Manager, HRIS Flow Platform", Remote.HYBRID),  # on-site and hybrid
        ("Account Executive, LATAM", Remote.HYBRID),  # Mexico City
        ("Senior Engineering Manager - Compliance", Remote.ONSITE),  # Bangalore
    ],
)
@respx.mock
async def test_workplace_type_per_location(http: HttpClient, title: str, remote: Remote) -> None:
    mock_board()
    assert (await board_jobs(http))[title].remote is remote


@respx.mock
async def test_mixed_locations_are_all_listed(http: HttpClient) -> None:
    mock_board()
    job = (await board_jobs(http))["Senior Manager, Support Operations"]
    assert job.locations == [
        "New York, NY",
        "San Francisco, CA",
        "Remote (United States)",
    ]


@respx.mock
async def test_pages_are_merged_and_overlaps_dropped(http: HttpClient) -> None:
    items = fixture_json("rippling_rippling.json")["items"]
    first = {"items": items[:7], "page": 0, "pageSize": 7, "totalItems": 12, "totalPages": 2}
    # Results come in no set order, so a job can show up on two pages.
    second = {"items": items[6:], "page": 1, "pageSize": 7, "totalItems": 12, "totalPages": 2}
    respx.get(RP.list_url(BOARD)).mock(return_value=httpx.Response(200, json=first))
    respx.get(RP.list_url(BOARD, 1)).mock(return_value=httpx.Response(200, json=second))
    listing = await RP.list_jobs(http, "Rippling", BOARD)
    assert len(listing.jobs) == 12 and listing.note is None


@respx.mock
async def test_pages_past_the_cap_are_noted(http: HttpClient) -> None:
    items = fixture_json("rippling_rippling.json")["items"]
    pages = MAX_PAGES + 2
    for page in range(MAX_PAGES):
        body = {"items": [items[page]], "page": page, "totalPages": pages}
        respx.get(RP.list_url(BOARD, page)).mock(return_value=httpx.Response(200, json=body))
    listing = await RP.list_jobs(http, "Rippling", BOARD)
    assert len(listing.jobs) == MAX_PAGES
    assert listing.note and f"{pages} pages" in listing.note


@respx.mock
async def test_get_job_and_check_live_use_the_board(http: HttpClient) -> None:
    mock_board()
    job_id = "ce9bf549-cd9d-4dcc-8121-98629fdeffd5"
    job = await RP.get_job(http, "Rippling", BOARD, job_id.upper())
    assert job.title == "Senior Manager, Support Operations"
    assert (await RP.check_live(http, BOARD, job_id)).live
    gone = "00000000-0000-0000-0000-000000000000"
    assert "no longer listed" in (await RP.check_live(http, BOARD, gone)).reason
    with pytest.raises(NotFound):
        await RP.get_job(http, "Rippling", BOARD, gone)


@respx.mock
async def test_empty_board(http: HttpClient) -> None:
    respx.get(RP.list_url("demo")).mock(
        return_value=httpx.Response(200, json=fixture_json("rippling_empty.json"))
    )
    listing = await RP.list_jobs(http, "Demo", "demo")
    assert listing.jobs == [] and listing.note is None


@respx.mock
async def test_unknown_board(http: HttpClient) -> None:
    respx.get(RP.list_url("nosuch")).mock(
        return_value=httpx.Response(404, json=fixture_json("rippling_404.json"))
    )
    with pytest.raises(NotFound):
        await RP.list_jobs(http, "Nosuch", "nosuch")
    assert not (await RP.check_live(http, "nosuch", "ce9bf549-cd9d-4dcc-8121-98629fdeffd5")).live


@respx.mock
async def test_probe_reads_the_total(http: HttpClient) -> None:
    body = {"items": [], "page": 0, "pageSize": 1, "totalItems": 364, "totalPages": 364}
    respx.get(url__startswith=f"https://api.rippling.com/platform/api/ats/v2/board/{BOARD}/").mock(
        return_value=httpx.Response(200, json=body)
    )
    probe = await RP.probe(http, BOARD)
    assert (probe.jobs, probe.names) == (364, set())


JOB = "84d388b6-7656-434c-8862-0312eb6b97ac"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://ats.rippling.com/rippling/jobs/{JOB}", ("rippling", JOB)),
        (f"https://ats.rippling.com/en-GB/rippling/jobs/{JOB}", ("rippling", JOB)),
        (f"https://ats.rippling.com/rippling/jobs/{JOB.upper()}/apply", ("rippling", JOB)),
        ("https://ats.rippling.com/rippling/jobs", None),
        (f"https://www.rippling.com/careers/open-roles/{JOB}", None),
    ],
)
def test_parse_url(url: str, expected: tuple[str, str] | None) -> None:
    ref = RP.parse_url(url)
    if expected is None:
        assert ref is None
    else:
        assert ref is not None and (ref.ats, ref.token, ref.job_id) == (Ats.RIPPLING, *expected)


def test_rippling_links_found_on_pages() -> None:
    html = (
        '<a href="https://ats.rippling.com/acme/jobs">Jobs</a>'
        f'<a href="https://ats.rippling.com/en-GB/globex/jobs/{JOB}">a job</a>'
        '<script>fetch("https://api.rippling.com/platform/api/ats/v2/board/initech/jobs")</script>'
    )
    refs = find_board_refs(html)
    assert {(Ats.RIPPLING, t) for t in ("acme", "globex", "initech")} <= set(refs)
    assert not any(t in ("en-gb", "jobs") for _, t in refs)
