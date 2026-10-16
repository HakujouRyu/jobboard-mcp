"""resolve_board against mocked APIs and careers-page snippets copied from real sites."""

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from jobboard_mcp.config import Settings
from jobboard_mcp.http import HttpClient, TextPage
from jobboard_mcp.models import Ats, Confidence
from jobboard_mcp.resolve import career_links, find_board_refs, resolve_board, token_candidates

# Snippets from real careers pages (2026-10-15), trimmed.
POSIT_ABOUT = (
    '<h2><span>Join us</span></h2><div class="text-formatted">'
    '<script src="https://boards.greenhouse.io/embed/job_board/js?for=rstudio"></script>'
    '<div id="grnhse_app">&nbsp;</div>'
)
POSIT_404 = '<p>Page not found</p><a href="/about/careers">Careers</a><a href="/about">About</a>'
INFLUX = '<div id="ashby_embed"></div>\n<script src="https://jobs.ashbyhq.com/Influxdata/embed"></script>'
MUX = '<a href="https://jobs.ashbyhq.com/mux/bd297d04-e5c5-4ee9-9a3f-db0951825e2d">Senior Full Stack</a>'
MAPBOX = '<a href="https://jobs.ashbyhq.com/Mapbox/form/company-interest" target="_blank">'
COLONIST = '<a class="btn-open-roles" href="https://jobs.ashbyhq.com/colonist?locationId=4cc9e45e">'
GITHUB = "user_id: '', // Pass unique user identifier from iCIMS backend <a href=\"https://careers-githubinc.icims.com/jobs/login\">"
SIGNOZ_HOME = (
    '<a href="/pricing">Pricing</a><a href="/careers/">Careers</a><a href="/blog">Blog</a>'
)

GH = "https://boards-api.greenhouse.io/v1/boards/"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/"
LEVER = "https://api.lever.co/v0/postings/"


def gh_url(token: str) -> str:
    return f"{GH}{token}/jobs?content=true"


def ashby_url(token: str) -> str:
    return f"{ASHBY}{token}?includeCompensation=true"


def lever_url(token: str) -> str:
    return f"{LEVER}{token}?mode=json"


def not_found_everywhere_else(router: respx.MockRouter) -> None:
    """Registered last: every other probe is an unknown token."""
    for host in (GH, ASHBY, LEVER):
        router.get(url__startswith=host).mock(return_value=httpx.Response(404, text="Not Found"))


@pytest.mark.parametrize(
    ("company", "url", "expected"),
    [
        ("Flower Labs", None, ["flowerlabs", "flower-labs", "flower"]),
        ("Posit", "https://posit.co/careers/", ["posit"]),
        ("Kong", "https://konghq.com/careers", ["kong", "konghq"]),
        (
            "Wikimedia Foundation",
            None,
            ["wikimediafoundation", "wikimedia-foundation", "wikimedia"],
        ),
        ("AT&T", None, ["atandt", "at-and-t", "att", "at-t", "at"]),
        (None, "https://careers.example.io/jobs", ["example"]),
    ],
)
def test_token_candidates(company: str | None, url: str | None, expected: list[str]) -> None:
    assert token_candidates(company, url) == expected


@pytest.mark.parametrize(
    ("html", "refs"),
    [
        (POSIT_ABOUT, [(Ats.GREENHOUSE, "rstudio")]),
        (INFLUX, [(Ats.ASHBY, "influxdata")]),
        (MUX, [(Ats.ASHBY, "mux")]),
        (MAPBOX, [(Ats.ASHBY, "mapbox")]),
        (COLONIST, [(Ats.ASHBY, "colonist")]),
        ('"url":"https:\\/\\/jobs.ashbyhq.com\\/acme\\/123"', [(Ats.ASHBY, "acme")]),
        ('<a href="https://job-boards.greenhouse.io/gitlab/jobs/1">', [(Ats.GREENHOUSE, "gitlab")]),
        ('<a href="https://boards.greenhouse.io/embed/job_app?for=co&amp;token=1">', []),
        ('<iframe src="https://boards.greenhouse.io/embed/job_board?b=x&amp;for=acme">',
         [(Ats.GREENHOUSE, "acme")]),
        ('<a href="https://jobs.lever.co/acme/abc-123/apply">', [(Ats.LEVER, "acme")]),
        ('<a href="https://jobs.eu.lever.co/acme">', [(Ats.LEVER, "acme")]),
        (GITHUB, []),
        (MUX + MUX + MAPBOX, [(Ats.ASHBY, "mux"), (Ats.ASHBY, "mapbox")]),
    ],
)  # fmt: skip
def test_find_board_refs(html: str, refs: list[tuple[Ats, str]]) -> None:
    assert find_board_refs(html) == refs


def test_career_links_same_site_ranked() -> None:
    page = TextPage(
        url="https://www.acme.io/",
        status=200,
        text=(
            '<a href="/work-with-us">x</a><a href="https://other.com/careers">x</a>'
            '<a href="/company/careers/">x</a><a href="/careers">x</a><a href="/blog">x</a>'
            '<a href="mailto:jobs@acme.io">x</a>'
        ),
    )
    assert career_links(page) == [
        "https://www.acme.io/careers",
        "https://www.acme.io/company/careers/",
    ]


@pytest.fixture
async def http() -> AsyncIterator[HttpClient]:
    client = HttpClient(Settings())
    yield client
    await client.aclose()


@respx.mock
async def test_posit_follows_link_from_404_page_to_embed(http: HttpClient) -> None:
    respx.get("https://posit.co/careers/").mock(return_value=httpx.Response(404, text=POSIT_404))
    respx.get("https://posit.co/about/careers").mock(
        return_value=httpx.Response(200, text=POSIT_ABOUT)
    )
    respx.get(gh_url("rstudio")).mock(
        return_value=httpx.Response(200, json={"jobs": [{"company_name": "Posit"}] * 8})
    )
    not_found_everywhere_else(respx.mock)
    r = await resolve_board(http, "Posit", "https://posit.co/careers/")
    assert (r.ats, r.token, r.confidence, r.jobs) == (Ats.GREENHOUSE, "rstudio", Confidence.HIGH, 8)
    assert any("links to greenhouse board 'rstudio'" in e for e in r.evidence)
    assert any("company name 'Posit'" in e for e in r.evidence)
    assert r.pages_checked == ["https://posit.co/careers/", "https://posit.co/about/careers"]


@respx.mock
async def test_signoz_follows_redirect_and_agrees_with_probe(http: HttpClient) -> None:
    respx.get("https://signoz.io/").mock(return_value=httpx.Response(200, text=SIGNOZ_HOME))
    respx.get("https://signoz.io/careers/").mock(
        return_value=httpx.Response(302, headers={"Location": "https://jobs.ashbyhq.com/SigNoz"})
    )
    respx.get("https://jobs.ashbyhq.com/SigNoz").mock(
        return_value=httpx.Response(200, text='<script>window.__appData = {"organization":'
                                    '{"hostedJobsPageSlug":"SigNoz"}}</script>'
                                    '<link href="https://jobs.ashbyhq.com/SigNoz/embed">')
    )  # fmt: skip
    respx.get(ashby_url("signoz")).mock(return_value=httpx.Response(200, json={"jobs": [{}] * 15}))
    not_found_everywhere_else(respx.mock)
    r = await resolve_board(http, "SigNoz", "https://signoz.io")
    assert (r.ats, r.token, r.confidence, r.jobs) == (Ats.ASHBY, "signoz", Confidence.HIGH, 15)
    assert r.pages_checked[1] == "https://signoz.io/careers/ -> https://jobs.ashbyhq.com/SigNoz"
    assert r.alternatives == []


@respx.mock
async def test_probe_only_ranks_and_lists_alternatives(http: HttpClient) -> None:
    respx.get(lever_url("acme")).mock(return_value=httpx.Response(200, json=[{}, {}, {}]))
    respx.get(ashby_url("acme")).mock(return_value=httpx.Response(200, json={"jobs": []}))
    not_found_everywhere_else(respx.mock)
    r = await resolve_board(http, "Acme Inc")
    assert (r.ats, r.token, r.confidence, r.jobs) == (Ats.LEVER, "acme", Confidence.MEDIUM, 3)
    assert [(a.ats, a.token, a.confidence) for a in r.alternatives] == [
        (Ats.ASHBY, "acme", Confidence.LOW)
    ]
    assert r.pages_checked == []


@respx.mock
async def test_greenhouse_name_mismatch_is_not_high(http: HttpClient) -> None:
    respx.get(gh_url("acme")).mock(
        return_value=httpx.Response(200, json={"jobs": [{"company_name": "ACME Freight Ltd"}]})
    )
    not_found_everywhere_else(respx.mock)
    r = await resolve_board(http, "Acme Robotics")
    assert (r.token, r.confidence) == ("acme", Confidence.MEDIUM)


@respx.mock
async def test_stale_link_and_unsupported_system(http: HttpClient) -> None:
    respx.get("https://oldco.test/careers").mock(
        return_value=httpx.Response(
            200,
            text='<a href="https://boards.greenhouse.io/oldco">old</a>'
            '<a href="https://oldco.wd5.myworkdayjobs.com/en-US/External">new</a>',
        )
    )
    not_found_everywhere_else(respx.mock)  # the old Greenhouse board is gone
    r = await resolve_board(http, "OldCo", "https://oldco.test/careers")
    assert r.ats is None and r.token is None and r.confidence is None
    assert any("myworkdayjobs.com" in e for e in r.evidence)


@respx.mock
async def test_unreachable_careers_page_still_probes(http: HttpClient) -> None:
    respx.get("https://down.test/careers").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(ashby_url("down")).mock(return_value=httpx.Response(200, json={"jobs": [{}]}))
    not_found_everywhere_else(respx.mock)
    r = await resolve_board(http, "Down", "https://down.test/careers")
    assert (r.ats, r.token, r.confidence) == (Ats.ASHBY, "down", Confidence.MEDIUM)
    assert r.pages_checked == ["https://down.test/careers"]


async def test_requires_company_or_url(http: HttpClient) -> None:
    with pytest.raises(ValueError, match="company and/or careers_url"):
        await resolve_board(http)
