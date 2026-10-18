"""Oracle Recruiting Cloud adapter against recorded careers-site responses."""

from datetime import date
from urllib.parse import unquote

import httpx
import pytest
import respx

from jobboard_mcp.adapters.oracle import MAX_PER_SEARCH, OracleAdapter
from jobboard_mcp.http import FetchError, HttpClient, NotFound
from jobboard_mcp.models import Ats, PaySource, Remote, Source
from jobboard_mcp.resolve import find_board_refs

from .conftest import fixture_json

ORC = OracleAdapter()
ATL = "ehxr.fa.us2/cx_1"
HOST = "https://ehxr.fa.us2.oraclecloud.com"
SEARCH = f"{HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAIL = f"{HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"


def finder(request: httpx.Request) -> str:
    return unquote(request.url.params["finder"])


def test_urls_from_token() -> None:
    assert finder(httpx.Request("GET", ORC.list_url(ATL))) == (
        "findReqs;siteNumber=CX_1,limit=200,offset=0,sortBy=POSTING_DATES_DESC"
    )
    assert finder(httpx.Request("GET", ORC.list_url(ATL, "engineer", 1))).endswith(
        'limit=1,offset=0,sortBy=POSTING_DATES_DESC,keyword="engineer"'
    )
    assert finder(httpx.Request("GET", ORC.detail_url(ATL, "34378"))) == (
        'ById;Id="34378",siteNumber=CX_1'
    )
    assert ORC.public_url("EHXR.fa.US2/CX_1", "34378") == (
        f"{HOST}/hcmUI/CandidateExperience/en/sites/CX_1/job/34378"
    )


@pytest.mark.parametrize("token", ["ehxr", "ehxr.fa.us2", "evil.example.com/CX_1", "a.fa.b/c d"])
def test_bad_tokens_are_refused(token: str) -> None:
    with pytest.raises(FetchError, match="not an Oracle Recruiting Cloud board token"):
        ORC.list_url(token)


def test_job_ids_must_be_numeric() -> None:
    with pytest.raises(NotFound):
        ORC.detail_url(ATL, '1",siteNumber=CX_2')


@respx.mock
async def test_list_builds_jobs_from_search_rows(http: HttpClient) -> None:
    route = respx.get(url__startswith=SEARCH).mock(
        return_value=httpx.Response(200, json=fixture_json("oracle_atlanta_search.json"))
    )
    listing = await ORC.list_jobs(http, "City of Atlanta", ATL, search=["engineer"])
    assert finder(route.calls.last.request).endswith(',keyword="engineer"')
    assert listing.note is None and len(listing.jobs) == 4
    job = listing.jobs[0]
    assert job.id == f"oracle:{ATL}:34378"
    assert (job.ats, job.source) == (Ats.ORACLE, Source.CAREERS_SITE)
    assert job.title == "Deputy, Chief Administrative Officer"
    assert job.locations == ["Atlanta, GA, United States"]
    # WorkplaceType is blank on this site, so a city alone doesn't make it on-site.
    assert job.remote is Remote.UNKNOWN
    assert (job.posted_at, job.date_source) == (date(2026, 10, 16), "PostedDate")
    assert job.url == f"{HOST}/hcmUI/CandidateExperience/en/sites/CX_1/job/34378"
    assert job.description_text is None and ORC.needs_detail(job)


@respx.mock
async def test_without_terms_the_keyword_is_left_out(http: HttpClient) -> None:
    route = respx.get(url__startswith=SEARCH).mock(
        return_value=httpx.Response(200, json=fixture_json("oracle_atlanta_search.json"))
    )
    await ORC.list_jobs(http, "City of Atlanta", ATL, search=["", "  "])
    assert "keyword" not in finder(route.calls.last.request)


@respx.mock
async def test_keywords_cant_break_out_of_the_finder(http: HttpClient) -> None:
    route = respx.get(url__startswith=SEARCH).mock(
        return_value=httpx.Response(200, json=fixture_json("oracle_atlanta_search.json"))
    )
    await ORC.list_jobs(http, "City of Atlanta", ATL, search=['eng",siteNumber=CX_9;x'])
    assert finder(route.calls.last.request).endswith(',keyword="eng siteNumber CX_9 x"')


@respx.mock
async def test_search_cap_is_noted(http: HttpClient) -> None:
    data = fixture_json("oracle_atlanta_search.json")
    row = data["items"][0]["requisitionList"][0]
    rows = [{**row, "Id": str(1000 + i)} for i in range(MAX_PER_SEARCH)]
    data["items"][0] |= {"requisitionList": rows, "TotalJobsCount": 2363}
    respx.get(url__startswith=SEARCH).mock(return_value=httpx.Response(200, json=data))
    listing = await ORC.list_jobs(http, "Oracle", ATL)
    assert len(listing.jobs) == MAX_PER_SEARCH
    assert listing.note is not None and "the whole board: 2363" in listing.note


@respx.mock
async def test_detail_adds_description_and_pay(http: HttpClient) -> None:
    respx.get(url__startswith=DETAIL).mock(
        return_value=httpx.Response(200, json=fixture_json("oracle_atlanta_detail.json"))
    )
    job = await ORC.get_job(http, "City of Atlanta", ATL, "34378")
    assert job.id == f"oracle:{ATL}:34378"
    assert job.description_text and "Salary Range" in job.description_text
    assert (job.pay_min, job.pay_max, job.pay_currency) == (117999, 157529, "USD")
    assert job.pay_source is PaySource.PARSED_FROM_TEXT
    assert (job.posted_at, job.date_source) == (date(2026, 10, 16), "ExternalPostedStartDate")
    assert job.team == "Operations"


@respx.mock
async def test_missing_job_is_not_found_and_not_live(http: HttpClient) -> None:
    respx.get(url__startswith=DETAIL).mock(
        return_value=httpx.Response(200, json=fixture_json("oracle_not_found.json"))
    )
    with pytest.raises(NotFound):
        await ORC.get_job(http, "City of Atlanta", ATL, "99999999")
    assert not (await ORC.check_live(http, ATL, "99999999")).live


@respx.mock
async def test_live_job(http: HttpClient) -> None:
    respx.get(url__startswith=DETAIL).mock(
        return_value=httpx.Response(200, json=fixture_json("oracle_atlanta_detail.json"))
    )
    assert (await ORC.check_live(http, ATL, "34378")).live


@respx.mock
async def test_probe_reports_total(http: HttpClient) -> None:
    route = respx.get(url__startswith=SEARCH).mock(
        return_value=httpx.Response(200, json=fixture_json("oracle_atlanta_search.json"))
    )
    probe = await ORC.probe(http, ATL)
    assert probe.jobs == 25
    assert "limit=1," in finder(route.calls.last.request)


@pytest.mark.parametrize(
    ("url", "token", "job_id"),
    [
        (f"{HOST}/hcmUI/CandidateExperience/en/sites/CX_1/job/34378", "ehxr.fa.us2/cx_1", "34378"),
        (
            f"{HOST}/hcmUI/CandidateExperience/sites/CX_1/job/34378/apply",
            "ehxr.fa.us2/cx_1",
            "34378",
        ),
        (
            "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en-US/sites/CX_45001/job/301234?utm=x",
            "eeho.fa.us2/cx_45001",
            "301234",
        ),
    ],
)
def test_parse_url(url: str, token: str, job_id: str) -> None:
    ref = ORC.parse_url(url)
    assert ref is not None and (ref.ats, ref.token, ref.job_id) == (Ats.ORACLE, token, job_id)


@pytest.mark.parametrize(
    "url",
    [
        f"{HOST}/hcmUI/CandidateExperience/en/sites/CX_1/requisitions",
        f"{HOST}/hcmUI/CandidateExperience/en/sites/CX_1/job/not-a-number",
        "https://ehxr.fa.us2.oraclecloud.com.evil.test/hcmUI/CandidateExperience/en/sites/CX_1/job/1",
        "https://example.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1",
    ],
)
def test_parse_url_rejects_other_urls(url: str) -> None:
    assert ORC.parse_url(url) is None


def test_oracle_links_found_on_pages() -> None:
    html = (
        '<a href="https://ehxr.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1">'
        '<a href="https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/sites/CX_45001/job/1">'
    )
    refs = find_board_refs(html)
    assert (Ats.ORACLE, "ehxr.fa.us2/cx_1") in refs
    assert (Ats.ORACLE, "eeho.fa.us2/cx_45001") in refs


@respx.mock
async def test_job_ids_ignore_token_case(http: HttpClient) -> None:
    respx.get(url__startswith=SEARCH).mock(
        return_value=httpx.Response(200, json=fixture_json("oracle_atlanta_search.json"))
    )
    upper = await ORC.list_jobs(http, "City of Atlanta", "EHXR.fa.us2/CX_1")
    ref = ORC.parse_url(f"{HOST}/hcmUI/CandidateExperience/en/sites/CX_1/job/34378")
    assert ref is not None
    assert upper.jobs[0].id == f"oracle:{ref.token}:{ref.job_id}" == f"oracle:{ATL}:34378"
