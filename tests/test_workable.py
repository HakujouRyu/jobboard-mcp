"""Workable adapter against a recorded accounts API response (Hugging Face)."""

from datetime import date

import httpx
import pytest
import respx

from jobboard_mcp.adapters.workable import WorkableAdapter
from jobboard_mcp.http import HttpClient, NotFound
from jobboard_mcp.models import Ats, EmploymentType, Remote, Source
from jobboard_mcp.resolve import find_board_refs

from .conftest import fixture_json, fixture_text

WK = WorkableAdapter()
HF = "huggingface"


def mock_board() -> respx.Route:
    return respx.get(WK.list_url(HF)).mock(
        return_value=httpx.Response(200, json=fixture_json("workable_huggingface.json"))
    )


@respx.mock
async def test_list_builds_jobs(http: HttpClient) -> None:
    mock_board()
    listing = await WK.list_jobs(http, "Hugging Face", HF)
    assert len(listing.jobs) == 6
    by_id = {j.native_id: j for j in listing.jobs}
    us = by_id["19A136F8E2"]
    assert us.id == "workable:huggingface:19A136F8E2"
    assert (us.ats, us.source) == (Ats.WORKABLE, Source.DOCUMENTED_API)
    assert us.title == "Open-Source Machine Learning Engineer - US Remote"
    assert us.locations == ["New York, New York, United States"]
    assert us.remote is Remote.REMOTE_US
    assert (us.posted_at, us.date_source) == (date(2026, 5, 29), "published_on")
    assert us.employment_type is EmploymentType.FULL_TIME
    assert us.team == "Open Source"
    assert us.url == "https://apply.workable.com/huggingface/j/19A136F8E2/"
    assert WK.parse_url(us.url) is not None
    assert us.apply_url == "https://apply.workable.com/j/19A136F8E2/apply"
    assert us.description_text and not WK.needs_detail(us)
    emea = by_id["81B46579FE"]
    assert emea.remote is Remote.REMOTE_OTHER  # remote, France only


@respx.mock
async def test_hidden_locations_keep_only_the_country(http: HttpClient) -> None:
    mock_board()
    by_id = {j.native_id: j for j in (await WK.list_jobs(http, "Hugging Face", HF)).jobs}
    assert by_id["9E2A4C02C7"].locations == ["France"]  # city hidden by the employer
    assert by_id["F88446C814"].locations == ["United States"]
    assert by_id["F88446C814"].remote is Remote.REMOTE_US


@respx.mock
async def test_get_job_and_check_live_use_the_board(http: HttpClient) -> None:
    mock_board()
    job = await WK.get_job(http, "Hugging Face", HF, "19a136f8e2")
    assert job.native_id == "19A136F8E2"
    assert (await WK.check_live(http, HF, "19A136F8E2")).live
    gone = await WK.check_live(http, HF, "ABCDEF1234")
    assert not gone.live and "no longer listed" in gone.reason
    with pytest.raises(NotFound):
        await WK.get_job(http, "Hugging Face", HF, "ABCDEF1234")


@respx.mock
async def test_unknown_account(http: HttpClient) -> None:
    respx.get(WK.list_url("nosuch")).mock(
        return_value=httpx.Response(404, text=fixture_text("workable_404.txt"))
    )
    with pytest.raises(NotFound):
        await WK.list_jobs(http, "Nosuch", "nosuch")
    assert not (await WK.check_live(http, "nosuch", "ABCDEF1234")).live


@respx.mock
async def test_probe_reports_jobs_and_account_name(http: HttpClient) -> None:
    mock_board()
    probe = await WK.probe(http, HF)
    assert (probe.jobs, probe.names) == (6, {"Hugging Face"})


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://apply.workable.com/huggingface/j/19A136F8E2/", ("huggingface", "19A136F8E2")),
        (
            "https://apply.workable.com/HuggingFace/j/19a136f8e2/apply",
            ("huggingface", "19A136F8E2"),
        ),
        ("https://apply.workable.com/j/19A136F8E2", None),  # no account in the short form
        ("https://apply.workable.com/huggingface/", None),
        ("https://jobs.ashbyhq.com/linear/0f6f5a3c-8c7c-4c1f-9f5b-0d1b1e2c3d4e", None),
    ],
)
def test_parse_url(url: str, expected: tuple[str, str] | None) -> None:
    ref = WK.parse_url(url)
    if expected is None:
        assert ref is None
    else:
        assert ref is not None and (ref.ats, ref.token, ref.job_id) == (Ats.WORKABLE, *expected)


def test_workable_links_found_on_pages() -> None:
    html = (
        '<a href="https://apply.workable.com/huggingface/">Jobs</a>'
        '<a href="https://apply.workable.com/j/19A136F8E2">a job</a>'
        '<script src="https://apply.workable.com/api/v1/widget/accounts/acme?details=true">'
    )
    refs = find_board_refs(html)
    assert (Ats.WORKABLE, "huggingface") in refs
    assert (Ats.WORKABLE, "acme") in refs
    assert not any(t == "j" for _, t in refs)
