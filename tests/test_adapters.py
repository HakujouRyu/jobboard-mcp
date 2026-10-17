from datetime import date

import httpx
import pytest
import respx

from jobboard_mcp.adapters import ADAPTERS, PostingRef, parse_posting_url
from jobboard_mcp.adapters.ashby import AshbyAdapter
from jobboard_mcp.adapters.greenhouse import GreenhouseAdapter
from jobboard_mcp.adapters.lever import LeverAdapter
from jobboard_mcp.http import HttpClient, NotFound
from jobboard_mcp.models import Ats, EmploymentType, PaySource, Remote

from .conftest import fixture_json, fixture_text

GH = GreenhouseAdapter()
ASHBY = AshbyAdapter()
LEVER = LeverAdapter()


def by_title(jobs, fragment):
    return next(j for j in jobs if fragment in j.title)


# ------------------------------------------------------------------ Greenhouse


@respx.mock
async def test_greenhouse_list(http: HttpClient) -> None:
    respx.get(GH.list_url("gitlab")).mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_gitlab_list.json"))
    )
    listing = await GH.list_jobs(http, "GitLab", "gitlab")
    jobs = listing.jobs
    assert len(jobs) == 16 and not listing.cache_hit

    fr = by_title(jobs, "Account Executive - France")
    assert fr.id == "greenhouse:gitlab:8860302002"
    assert fr.locations == ["Remote, France"]
    assert fr.remote is Remote.REMOTE_OTHER
    assert fr.posted_at == date(2026, 10, 2) and fr.date_source == "first_published"
    assert fr.team == "EMEA - Commercial"
    assert fr.url == "https://job-boards.greenhouse.io/gitlab/jobs/8860302002"
    assert fr.employment_type is None  # Greenhouse has no structured employment type

    multi = next(j for j in jobs if len(j.locations) > 1 and "Remote, United States" in j.locations)
    assert multi.remote is Remote.REMOTE_US

    bare = next(j for j in jobs if j.locations == ["Remote"])
    assert bare.remote is Remote.UNKNOWN

    india = next(j for j in jobs if j.locations == ["Bangalore, India"])
    assert india.remote is Remote.ONSITE


@respx.mock
async def test_greenhouse_pay_parsed_from_list_content(http: HttpClient) -> None:
    respx.get(GH.list_url("gitlab")).mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_gitlab_list.json"))
    )
    jobs = (await GH.list_jobs(http, "GitLab", "gitlab")).jobs
    paid = [j for j in jobs if j.pay_source is PaySource.PARSED_FROM_TEXT]
    assert paid, "fixture should include US roles with a salary range in the body"
    for j in paid:
        assert j.pay_currency == "USD" and j.pay_min and j.pay_max and j.pay_min <= j.pay_max
        assert j.pay_raw and "$" in j.pay_raw
        assert not GH.needs_detail_for_pay(j)
    unpaid = next(j for j in jobs if j.pay_source is PaySource.NONE)
    assert GH.needs_detail_for_pay(unpaid)


@respx.mock
async def test_greenhouse_detail_structured_pay(http: HttpClient) -> None:
    detail = fixture_json("greenhouse_gitlab_detail.json")
    respx.get(GH.detail_url("gitlab", str(detail["id"]))).mock(
        return_value=httpx.Response(200, json=detail)
    )
    job = await GH.get_job(http, "GitLab", "gitlab", str(detail["id"]))
    assert job.pay_source is PaySource.STRUCTURED
    assert (job.pay_min, job.pay_max) == (139_200, 235_200)
    assert job.pay_currency == "USD" and job.pay_posted_interval == "year"
    assert job.pay_raw and "United States Salary Range" in job.pay_raw
    assert job.description_text and "&lt;" not in job.description_text
    assert "<p>" not in job.description_text


@respx.mock
async def test_greenhouse_detail_without_pay_ranges_falls_back_to_text(http: HttpClient) -> None:
    detail = fixture_json("greenhouse_gitlab_detail_nopay.json")
    respx.get(GH.detail_url("gitlab", str(detail["id"]))).mock(
        return_value=httpx.Response(200, json=detail)
    )
    job = await GH.get_job(http, "GitLab", "gitlab", str(detail["id"]))
    assert job.pay_source is not PaySource.STRUCTURED
    assert job.pay_min is None


@respx.mock
async def test_greenhouse_404(http: HttpClient) -> None:
    respx.get(GH.list_url("nope")).mock(
        return_value=httpx.Response(404, json=fixture_json("greenhouse_404.json"))
    )
    with pytest.raises(NotFound):
        await GH.list_jobs(http, "Nope", "nope")


# ------------------------------------------------------------------ Ashby


@respx.mock
async def test_ashby_linear_regions_and_no_pay(http: HttpClient) -> None:
    respx.get(ASHBY.list_url("linear")).mock(
        return_value=httpx.Response(200, json=fixture_json("ashby_linear.json"))
    )
    jobs = (await ASHBY.list_jobs(http, "Linear", "linear")).jobs
    assert len(jobs) == 6
    na = next(j for j in jobs if j.locations == ["North America"])
    assert na.remote is Remote.REMOTE_US
    eu = next(j for j in jobs if j.locations == ["Europe"])
    assert eu.remote is Remote.REMOTE_OTHER
    assert all(j.pay_source is PaySource.NONE for j in jobs)
    first = jobs[0]
    assert first.id.startswith("ashby:linear:") and first.date_source == "publishedAt"
    assert first.apply_url and first.apply_url.endswith("/application")
    assert first.employment_type is EmploymentType.FULL_TIME
    assert first.description_text


@respx.mock
async def test_ashby_temporal_pay_tiers_and_hybrid(http: HttpClient) -> None:
    respx.get(ASHBY.list_url("temporal")).mock(
        return_value=httpx.Response(200, json=fixture_json("ashby_temporal.json"))
    )
    jobs = (await ASHBY.list_jobs(http, "Temporal Technologies", "temporal")).jobs

    tiered = next(j for j in jobs if j.pay_raw and "US - Tier 2" in j.pay_raw)
    assert tiered.pay_source is PaySource.STRUCTURED and tiered.pay_currency == "USD"
    assert tiered.pay_min and tiered.pay_max and tiered.pay_min < tiered.pay_max
    assert "Equity" not in (tiered.pay_raw or "")  # salary components only

    # GBP, EUR, INR, and SGD roles keep their own numbers; nothing is converted.
    non_usd = [j for j in jobs if j.pay_currency not in (None, "USD")]
    assert {j.pay_currency for j in non_usd} >= {"GBP", "EUR"}
    assert all(j.pay_min and j.pay_max and j.pay_min <= j.pay_max for j in non_usd)
    assert all(not j.flags or "pay_unknown" not in j.flags for j in non_usd)

    # isRemote=true on hybrid roles: workplaceType must win.
    hybrid = [j for j in jobs if j.remote is Remote.HYBRID]
    assert hybrid
    assert all(j.remote is not Remote.REMOTE_US for j in hybrid)


@respx.mock
async def test_ashby_get_job_and_missing_job(http: HttpClient) -> None:
    data = fixture_json("ashby_linear.json")
    respx.get(ASHBY.list_url("linear")).mock(return_value=httpx.Response(200, json=data))
    wanted = data["jobs"][1]["id"]
    job = await ASHBY.get_job(http, "Linear", "linear", wanted)
    assert job.native_id == wanted
    with pytest.raises(NotFound):
        await ASHBY.get_job(http, "Linear", "linear", "00000000-0000-0000-0000-000000000000")


@respx.mock
async def test_ashby_404(http: HttpClient) -> None:
    respx.get(ASHBY.list_url("nope")).mock(
        return_value=httpx.Response(404, text=fixture_text("ashby_404.txt"))
    )
    with pytest.raises(NotFound):
        await ASHBY.list_jobs(http, "Nope", "nope")


# ------------------------------------------------------------------ Lever


@respx.mock
async def test_lever_outreach(http: HttpClient) -> None:
    respx.get(LEVER.list_url("outreach")).mock(
        return_value=httpx.Response(200, json=fixture_json("lever_outreach.json"))
    )
    jobs = (await LEVER.list_jobs(http, "Outreach", "outreach")).jobs
    assert len(jobs) == 8

    cam = by_title(jobs, "Customer Advocacy Manager")
    assert (cam.pay_min, cam.pay_max, cam.pay_source) == (70_000, 110_000, PaySource.STRUCTURED)
    assert cam.pay_posted_interval == "year" and cam.remote is Remote.REMOTE_US
    assert cam.employment_type is EmploymentType.FULL_TIME and cam.date_source == "createdAt"
    assert cam.url.startswith("https://jobs.lever.co/outreach/")
    assert cam.apply_url and cam.apply_url.endswith("/apply")

    india = by_title(jobs, "Forward Deployed Engineer")
    assert india.remote is Remote.REMOTE_OTHER  # remote, country IN

    hybrid = by_title(jobs, "Renewals Specialist")
    assert hybrid.remote is Remote.HYBRID
    assert hybrid.description_text


@respx.mock
async def test_lever_demo_unspecified_and_odd_commitments(http: HttpClient) -> None:
    respx.get(LEVER.list_url("leverdemo")).mock(
        return_value=httpx.Response(200, json=fixture_json("lever_leverdemo.json"))
    )
    jobs = (await LEVER.list_jobs(http, "Lever Demo", "leverdemo")).jobs
    baltimore = next(j for j in jobs if j.locations == ["Baltimore, MD"])
    assert baltimore.remote is Remote.UNKNOWN
    assert EmploymentType.UNKNOWN in {j.employment_type for j in jobs}  # commitment "Remote"


@respx.mock
async def test_lever_empty_board_and_404(http: HttpClient) -> None:
    respx.get(LEVER.list_url("empty")).mock(
        return_value=httpx.Response(200, json=fixture_json("lever_empty.json"))
    )
    respx.get(LEVER.list_url("nope")).mock(
        return_value=httpx.Response(404, json=fixture_json("lever_404.json"))
    )
    assert (await LEVER.list_jobs(http, "Empty", "empty")).jobs == []
    with pytest.raises(NotFound):
        await LEVER.list_jobs(http, "Nope", "nope")


@respx.mock
async def test_lever_get_job(http: HttpClient) -> None:
    raw = fixture_json("lever_outreach.json")[0]
    respx.get(LEVER.detail_url("outreach", raw["id"])).mock(
        return_value=httpx.Response(200, json=raw)
    )
    job = await LEVER.get_job(http, "Outreach", "outreach", raw["id"])
    assert job.native_id == raw["id"]


# ------------------------------------------------------------------ URL parsing


@pytest.mark.parametrize(
    ("url", "ref"),
    [
        ("https://job-boards.greenhouse.io/gitlab/jobs/8860302002",
         PostingRef(Ats.GREENHOUSE, "gitlab", "8860302002")),
        ("https://boards.greenhouse.io/gitlab/jobs/123?gh_src=x",
         PostingRef(Ats.GREENHOUSE, "gitlab", "123")),
        ("https://boards.greenhouse.io/embed/job_app?for=gitlab&token=456",
         PostingRef(Ats.GREENHOUSE, "gitlab", "456")),
        ("https://boards-api.greenhouse.io/v1/boards/gitlab/jobs/789",
         PostingRef(Ats.GREENHOUSE, "gitlab", "789")),
        ("https://jobs.ashbyhq.com/linear/D3BC1CED-3ce4-4086-a050-555055dbb1ff/application",
         PostingRef(Ats.ASHBY, "linear", "d3bc1ced-3ce4-4086-a050-555055dbb1ff")),
        ("https://jobs.lever.co/spotify/223f9722-3579-4ff2-b123-e4cc7b0c1a64/apply",
         PostingRef(Ats.LEVER, "spotify", "223f9722-3579-4ff2-b123-e4cc7b0c1a64")),
        ("https://www.linkedin.com/jobs/view/123", None),
        ("https://jobs.ashbyhq.com/linear", None),
    ],
)  # fmt: skip
def test_parse_posting_url(url: str, ref: PostingRef | None) -> None:
    assert parse_posting_url(url) == ref


def test_registry_covers_every_ats() -> None:
    assert set(ADAPTERS) == set(Ats)


@respx.mock
async def test_listing_memo_reuses_jobs_while_cached(http: HttpClient) -> None:
    route = respx.get(LEVER.list_url("outreach")).mock(
        side_effect=[
            httpx.Response(200, json=fixture_json("lever_outreach.json")),
            httpx.Response(200, json=fixture_json("lever_outreach.json")),
        ]
    )
    first = await LEVER.list_jobs(http, "Outreach", "outreach")
    second = await LEVER.list_jobs(http, "Outreach", "outreach")
    assert second.cache_hit and second.jobs is first.jobs
    fresh = await LEVER.list_jobs(http, "Outreach", "outreach", refresh=True)
    assert fresh.jobs is not first.jobs and fresh.jobs == first.jobs
    assert route.call_count == 2


# ------------------------------------------------------------------ shared job building


def test_build_job_flags_urls_and_title_signals() -> None:
    # Shaped like Wikimedia's and CircleCI's live postings (2026-10-16)
    raw = {
        "id": 8268142,
        "title": "Senior Software Engineer, Android (Contract)",
        "location": {"name": "Remote"},
        "absolute_url": "http://www.circleci.com/careers/jobs/8268142/?gh_jid=8268142",
        "content": "<p>$56.07 to US$87.13 hourly</p>",
    }
    job = GH.to_job(raw, "Wikimedia Foundation", "wikimedia")
    assert job.url.startswith("https://www.circleci.com/")
    assert job.employment_type is EmploymentType.CONTRACT
    # Flags come from the job itself, so get_posting and sweeps agree
    assert job.remote is Remote.UNKNOWN
    assert job.flags == ["date_unknown", "remote_unknown"]
    assert (job.pay_min, job.pay_posted_interval) == (116_626, "hour")


def test_build_job_flags_missing_pay_and_date() -> None:
    raw = {"id": 1, "title": "Engineer", "location": {"name": "Remote, US"}}
    assert GH.to_job(raw, "Acme", "acme").flags == ["pay_unknown", "date_unknown"]
