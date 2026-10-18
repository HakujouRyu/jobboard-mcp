"""Workday adapter against recorded careers-site responses (tests/fixtures/workday_*)."""

import json
from datetime import date

import httpx
import pytest
import respx

from jobboard_mcp.adapters.workday import MAX_PER_SEARCH, PAGE_SIZE, WorkdayAdapter
from jobboard_mcp.http import FetchError, HttpClient, NotFound
from jobboard_mcp.models import Ats, EmploymentType, PaySource, Remote, Source
from jobboard_mcp.normalize import parse_relative_posted, workplace_from_label

from .conftest import fixture_json

WD = WorkdayAdapter()
COX = "cox.wd1/cox_external_career_site_1"
COX_API = "https://cox.wd1.myworkdayjobs.com/wday/cxs/cox/cox_external_career_site_1"
HD = "homedepot.wd5/careerdepot"
HD_API = "https://homedepot.wd5.myworkdayjobs.com/wday/cxs/homedepot/careerdepot"
TODAY = date(2026, 10, 17)


def rows(n: int, start: int = 0) -> list[dict]:
    """Synthetic list rows shaped like the recorded ones."""
    return [
        {
            "title": f"Engineer {i}",
            "externalPath": f"/job/Atlanta-GA/Engineer-{i}_R{i:06d}",
            "timeType": "Full time",
            "locationsText": "Atlanta GA",
            "postedOn": "Posted 30+ Days Ago",
            "bulletFields": [f"R{i:06d}"],
        }
        for i in range(start, start + n)
    ]


def test_urls_from_token() -> None:
    assert WD.list_url(COX) == f"{COX_API}/jobs"
    assert WD.detail_url(COX, "X_R1") == f"{COX_API}/job/X_R1"
    assert WD.list_url("HomeDepot.WD5/CareerDepot") == f"{HD_API}/jobs"


@pytest.mark.parametrize("token", ["cox", "cox/site", "cox.wd1", "evil.example.com/x", "a.wd1/b c"])
def test_bad_tokens_are_refused(token: str) -> None:
    with pytest.raises(FetchError, match="not a Workday board token"):
        WD.list_url(token)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Remote/Virtual", "remote"),
        ("Fully Remote", "remote"),
        ("Hybrid", "hybrid"),
        ("Onsite", "onsite"),
        ("On-site", "onsite"),
        ("Multi-site", "unspecified"),
        ("Location Independent-Commutable", "unspecified"),
        ("ORA_ON_SITE", "onsite"),
        ("ORA_REMOTE", "remote"),
        (None, "unspecified"),
    ],
)
def test_workplace_labels(label: str | None, expected: str) -> None:
    assert workplace_from_label(label) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Posted Today", date(2026, 10, 17)),
        ("Posted Yesterday", date(2026, 10, 16)),
        ("Posted 10 Days Ago", date(2026, 10, 7)),
        ("posted 1 day ago", date(2026, 10, 16)),
        ("Posted 30+ Days Ago", None),
        ("", None),
        (None, None),
    ],
)
def test_relative_posted(text: str | None, expected: date | None) -> None:
    assert parse_relative_posted(text, TODAY) == expected


@respx.mock
async def test_list_searches_and_builds_listed_jobs(http: HttpClient) -> None:
    route = respx.post(f"{COX_API}/jobs").mock(
        return_value=httpx.Response(200, json=fixture_json("workday_cox_search.json"))
    )
    listing = await WD.list_jobs(http, "Cox", COX, search=["software engineer"], today=TODAY)
    assert json.loads(route.calls.last.request.content) == {
        "appliedFacets": {},
        "limit": PAGE_SIZE,
        "offset": 0,
        "searchText": "software engineer",
    }
    assert listing.note is None and len(listing.jobs) == 4
    job = listing.jobs[0]
    assert job.id == f"workday:{COX}:Senior-Software-Engineer_R202683513"
    assert (job.ats, job.source) == (Ats.WORKDAY, Source.CAREERS_SITE)
    assert job.title == "Senior Software Engineer"
    assert job.locations == ["Atlanta GA"]
    assert job.remote is Remote.UNKNOWN  # no remoteType in list rows: never on-site from a city
    assert (job.posted_at, job.date_source) == (date(2026, 10, 7), "postedOn")
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.url == (
        "https://cox.wd1.myworkdayjobs.com/cox_external_career_site_1"
        "/job/Atlanta-GA/Senior-Software-Engineer_R202683513"
    )
    assert job.description_text is None and WD.needs_detail(job)


@respx.mock
async def test_list_without_today_leaves_dates_unknown(http: HttpClient) -> None:
    respx.post(f"{COX_API}/jobs").mock(
        return_value=httpx.Response(200, json=fixture_json("workday_cox_search.json"))
    )
    listing = await WD.list_jobs(http, "Cox", COX)
    assert all(j.posted_at is None for j in listing.jobs)


@respx.mock
async def test_location_count_summary_is_not_a_location(http: HttpClient) -> None:
    row = rows(1)[0] | {"locationsText": "20 Locations"}
    respx.post(f"{COX_API}/jobs").mock(
        return_value=httpx.Response(200, json={"total": 1, "jobPostings": [row]})
    )
    (job,) = (await WD.list_jobs(http, "Cox", COX)).jobs
    assert job.locations == []


@respx.mock
async def test_list_pages_until_short_page(http: HttpClient) -> None:
    pages = {0: rows(20), 20: rows(20, 20), 40: rows(5, 40)}

    def answer(request: httpx.Request) -> httpx.Response:
        offset = json.loads(request.content)["offset"]
        total = 45 if offset == 0 else 0  # later pages don't always repeat the total
        return httpx.Response(200, json={"total": total, "jobPostings": pages[offset]})

    route = respx.post(f"{COX_API}/jobs").mock(side_effect=answer)
    listing = await WD.list_jobs(http, "Cox", COX, search=["engineer"])
    assert len(listing.jobs) == 45 and route.call_count == 3 and listing.note is None


@respx.mock
async def test_list_stops_at_the_cap_and_says_so(http: HttpClient) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        offset = json.loads(request.content)["offset"]
        return httpx.Response(200, json={"total": 980, "jobPostings": rows(20, offset)})

    route = respx.post(f"{COX_API}/jobs").mock(side_effect=answer)
    listing = await WD.list_jobs(http, "Cox", COX)
    assert len(listing.jobs) == MAX_PER_SEARCH
    assert route.call_count == MAX_PER_SEARCH // PAGE_SIZE
    assert listing.note is not None and "the whole board: 980" in listing.note


@respx.mock
async def test_each_term_is_searched_once_and_results_merged(http: HttpClient) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        term = json.loads(request.content)["searchText"]
        found = rows(3) if term == "engineer" else rows(3, 2)  # job 2 matches both
        return httpx.Response(200, json={"total": 3, "jobPostings": found})

    route = respx.post(f"{COX_API}/jobs").mock(side_effect=answer)
    listing = await WD.list_jobs(http, "Cox", COX, search=["engineer", " sre ", "engineer", ""])
    searched = [json.loads(c.request.content)["searchText"] for c in route.calls]
    assert searched == ["engineer", "sre"]
    assert [j.title for j in listing.jobs] == [f"Engineer {i}" for i in range(5)]


@respx.mock
async def test_missing_site_is_not_found(http: HttpClient) -> None:
    respx.post(f"{COX_API}/jobs").mock(
        return_value=httpx.Response(404, json=fixture_json("workday_404.json"))
    )
    with pytest.raises(NotFound):
        await WD.list_jobs(http, "Cox", COX)


@respx.mock
async def test_missing_tenant_422_is_not_found(http: HttpClient) -> None:
    respx.post(f"{COX_API}/jobs").mock(
        return_value=httpx.Response(422, json=fixture_json("workday_422.json"))
    )
    with pytest.raises(NotFound, match="no such Workday tenant"):
        await WD.list_jobs(http, "Cox", COX)
    with pytest.raises(NotFound):
        await WD.probe(http, COX)


@respx.mock
async def test_probe_reports_total(http: HttpClient) -> None:
    route = respx.post(f"{COX_API}/jobs").mock(
        return_value=httpx.Response(200, json=fixture_json("workday_cox_search.json"))
    )
    probe = await WD.probe(http, COX)
    assert (probe.jobs, probe.names) == (12, set())
    assert json.loads(route.calls.last.request.content)["limit"] == 1


@respx.mock
async def test_detail_without_remote_type(http: HttpClient) -> None:
    respx.get(f"{COX_API}/job/Senior-Software-Engineer_R202683513").mock(
        return_value=httpx.Response(200, json=fixture_json("workday_cox_detail.json"))
    )
    job = await WD.get_job(http, "Cox", COX, "Senior-Software-Engineer_R202683513")
    assert job.id == f"workday:{COX}:Senior-Software-Engineer_R202683513"
    assert job.locations == ["Atlanta GA"]
    # Cox doesn't use remoteType, and a city alone doesn't make a Workday job on-site.
    assert job.remote is Remote.UNKNOWN
    assert (job.posted_at, job.date_source) == (date(2026, 10, 7), "startDate")
    assert (job.pay_min, job.pay_max, job.pay_currency) == (101500, 169100, "USD")
    assert job.pay_source is PaySource.PARSED_FROM_TEXT
    assert job.description_text and "Cox" in job.description_text
    assert job.url.startswith("https://cox.wd1.myworkdayjobs.com/Cox_External_Career_Site_1/job/")


@respx.mock
async def test_detail_with_remote_type(http: HttpClient) -> None:
    job_id = "SENIOR-SOFTWARE-ENGINEER---Workday_Req186273"
    respx.get(f"{HD_API}/job/{job_id}").mock(
        return_value=httpx.Response(200, json=fixture_json("workday_homedepot_detail.json"))
    )
    job = await WD.get_job(http, "The Home Depot", HD, job_id)
    assert job.locations == ["GEORGIA - VIRTUAL - GA01"]
    assert job.remote is Remote.REMOTE_US  # "Remote/Virtual", country United States of America
    assert (job.pay_min, job.pay_max) == (120000, 140000)
    assert job.posted_at == date(2026, 7, 10)


@respx.mock
async def test_check_live(http: HttpClient) -> None:
    detail = fixture_json("workday_cox_detail.json")
    unposted = {**detail, "jobPostingInfo": {**detail["jobPostingInfo"], "posted": False}}
    respx.get(f"{COX_API}/job/live").mock(return_value=httpx.Response(200, json=detail))
    respx.get(f"{COX_API}/job/gone").mock(
        return_value=httpx.Response(404, json=fixture_json("workday_404.json"))
    )
    respx.get(f"{COX_API}/job/unposted").mock(return_value=httpx.Response(200, json=unposted))
    assert (await WD.check_live(http, COX, "live")).live
    gone = await WD.check_live(http, COX, "gone")
    assert not gone.live and "404" in gone.reason
    assert not (await WD.check_live(http, COX, "unposted")).live


@pytest.mark.parametrize(
    ("url", "token", "job_id"),
    [
        (
            "https://cox.wd1.myworkdayjobs.com/Cox_External_Career_Site_1/job/Atlanta-GA/Senior-Software-Engineer_R202683513",
            COX,
            "Senior-Software-Engineer_R202683513",
        ),
        (
            "https://homedepot.wd5.myworkdayjobs.com/en-US/CareerDepot/job/GEORGIA---VIRTUAL---GA01/SENIOR-SOFTWARE-ENGINEER---Workday_Req186273/apply",
            HD,
            "SENIOR-SOFTWARE-ENGINEER---Workday_Req186273",
        ),
        (
            "https://homedepot.wd5.myworkdayjobs.com/wday/cxs/homedepot/CareerDepot/job/X_R1",
            HD,
            "X_R1",
        ),
        (
            "https://wd5.myworkdaysite.com/en-US/recruiting/homedepot/CareerDepot/job/Atlanta/X_R1",
            HD,
            "X_R1",
        ),
    ],
)
def test_parse_url(url: str, token: str, job_id: str) -> None:
    ref = WD.parse_url(url)
    assert ref is not None and (ref.ats, ref.token, ref.job_id) == (Ats.WORKDAY, token, job_id)


@pytest.mark.parametrize(
    "url",
    [
        "https://cox.wd1.myworkdayjobs.com/Cox_External_Career_Site_1",
        "https://cox.wd1.myworkdayjobs.com/Cox_External_Career_Site_1/details/X",
        "https://cox.wd1.myworkdayjobs.com.evil.test/Site/job/X",
        "https://wd5.myworkdaysite.com/recruiting/homedepot",
        "https://jobs.lever.co/outreach/0f6f5a3c-8c7c-4c1f-9f5b-0d1b1e2c3d4e",
    ],
)
def test_parse_url_rejects_other_urls(url: str) -> None:
    assert WD.parse_url(url) is None


@respx.mock
async def test_search_with_no_matches(http: HttpClient) -> None:
    respx.post(f"{COX_API}/jobs").mock(
        return_value=httpx.Response(200, json=fixture_json("workday_empty.json"))
    )
    listing = await WD.list_jobs(http, "Cox", COX, search=["zzqxjv"])
    assert (listing.jobs, listing.note) == ([], None)
