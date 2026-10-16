"""End-to-end tool calls through the MCP server, against fixtures only (no network)."""

import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from jobboard_mcp import server
from jobboard_mcp.adapters.ashby import AshbyAdapter
from jobboard_mcp.adapters.greenhouse import GreenhouseAdapter
from jobboard_mcp.adapters.lever import LeverAdapter
from jobboard_mcp.config import Settings
from jobboard_mcp.http import HttpClient
from jobboard_mcp.models import Ats, WatchlistEntry
from jobboard_mcp.service import JobBoardService

from .conftest import fixture_json, fixture_text

NOW = datetime(2026, 10, 15, 18, 0, tzinfo=UTC)
GH, ASHBY, LEVER = GreenhouseAdapter(), AshbyAdapter(), LeverAdapter()

# 15 watched companies: real fixtures reused under several tokens, plus failure cases.
BOARDS: list[tuple[str, Ats | None, str | None, str]] = [
    ("GitLab", Ats.GREENHOUSE, "gitlab", "greenhouse_gitlab_list.json"),
    ("GitLab Two", Ats.GREENHOUSE, "gitlab2", "greenhouse_gitlab_list.json"),
    ("GitLab Three", Ats.GREENHOUSE, "gitlab3", "greenhouse_gitlab_list.json"),
    ("Linear", Ats.ASHBY, "linear", "ashby_linear.json"),
    ("Temporal Technologies", Ats.ASHBY, "temporal", "ashby_temporal.json"),
    ("Temporal Two", Ats.ASHBY, "temporal2", "ashby_temporal.json"),
    ("Linear Two", Ats.ASHBY, "linear2", "ashby_linear.json"),
    ("Outreach", Ats.LEVER, "outreach", "lever_outreach.json"),
    ("Outreach Two", Ats.LEVER, "outreach2", "lever_outreach.json"),
    ("Lever Demo", Ats.LEVER, "leverdemo", "lever_leverdemo.json"),
    ("Empty Co", Ats.LEVER, "empty", "lever_empty.json"),
    ("Gone Co", Ats.GREENHOUSE, "gone", "404"),
    ("Slow Co", Ats.ASHBY, "slow", "timeout"),
    ("Broken Co", Ats.LEVER, "broken", "500"),
    ("Careers Page Co", None, None, ""),
]


def _url(ats: Ats, token: str) -> str:
    return {Ats.GREENHOUSE: GH, Ats.ASHBY: ASHBY, Ats.LEVER: LEVER}[ats].list_url(token)


def mock_boards(router: respx.MockRouter) -> None:
    for _company, ats, token, fixture in BOARDS:
        if ats is None or token is None:
            continue
        route = router.get(_url(ats, token))
        if fixture == "404":
            route.mock(return_value=httpx.Response(404, json=fixture_json("greenhouse_404.json")))
        elif fixture == "timeout":
            route.mock(side_effect=httpx.ReadTimeout("slow"))
        elif fixture == "500":
            route.mock(return_value=httpx.Response(500, text="oops"))
        else:
            route.mock(return_value=httpx.Response(200, json=fixture_json(fixture)))
    # Greenhouse pay-transparency details for jobs whose body has no salary range.
    router.get(url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/[^/]+/jobs/\d+").mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_gitlab_detail.json"))
    )


@pytest.fixture
async def service() -> AsyncIterator[JobBoardService]:
    async def no_sleep(_s: float) -> None:
        return None

    entries = [WatchlistEntry(company=c, ats=a, token=t) for c, a, t, _ in BOARDS]
    svc = JobBoardService(
        Settings(),
        HttpClient(Settings(), sleep=no_sleep),
        watchlist=lambda: entries,
        now=lambda: NOW,
    )
    server.set_service(svc)
    yield svc
    server.set_service(None)
    await svc.aclose()


async def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await server.mcp.call_tool(name, args)
    assert isinstance(result, CallToolResult)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


@respx.mock(assert_all_called=False)
async def test_sweep_all_boards_offline_under_one_second(
    respx_mock: respx.MockRouter, service: JobBoardService
) -> None:
    mock_boards(respx_mock)
    started = time.perf_counter()
    out = await call("sweep_boards", {})
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"sweep took {elapsed:.2f}s"

    status = {c["company"]: c["status"] for c in out["per_company"]}
    assert status["Gone Co"] == "not_found"
    assert status["Slow Co"] == "timeout"
    assert status["Broken Co"] == "error"
    assert status["Careers Page Co"] == "no_board"
    assert status["Empty Co"] == "ok"
    assert sum(s == "ok" for s in status.values()) == 11
    assert [c["company"] for c in out["per_company"]] == [b[0] for b in BOARDS]

    jobs = out["jobs"]
    assert jobs
    assert {j["remote"] for j in jobs} <= {"remote_us", "remote_global", "unknown"}
    assert all("description_text" not in j or j["description_text"] is None for j in jobs)
    # Newest first, unknown dates last.
    dates = [j["posted_at"] for j in jobs if j["posted_at"]]
    assert dates == sorted(dates, reverse=True)
    assert len({j["id"] for j in jobs}) == len(jobs)
    unknown = [j for j in jobs if j["remote"] == "unknown"]
    assert unknown and all("remote_unknown" in j["flags"] for j in unknown)
    assert out["flag_counts"]["remote_unknown"] == len(unknown)


@respx.mock(assert_all_called=False)
async def test_sweep_acceptance_filters(
    respx_mock: respx.MockRouter, service: JobBoardService
) -> None:
    mock_boards(respx_mock)
    # SPEC §9 acceptance filters, except a 60-day window: the trimmed fixtures hold no
    # matching role from the last 21 days before NOW. The live run uses 21.
    out = await call(
        "sweep_boards",
        {
            "title_include": ["engineer", "sre", "product"],
            "remote": ["remote_us", "remote_global"],
            "min_pay": 100_000,
            "posted_within_days": 60,
        },
    )
    jobs = out["jobs"]
    assert jobs
    pattern = re.compile(r"\b(engineers?|sres?|products?)\b", re.I)
    for j in jobs:
        assert pattern.search(j["title"]), j["title"]
        assert j["remote"] in ("remote_us", "remote_global", "unknown")
        assert j["pay_max"] is None or j["pay_max"] >= 100_000
        assert j["posted_at"] is None or j["posted_at"] >= "2026-08-16"
        if j["pay_max"] is None:
            assert "pay_unknown" in j["flags"]
    by_company = {c["company"]: c for c in out["per_company"]}
    assert by_company["GitLab"]["matched"] == sum(j["company"] == "GitLab" for j in jobs)


@respx.mock(assert_all_called=False)
async def test_sweep_enriches_greenhouse_pay_from_detail(
    respx_mock: respx.MockRouter, service: JobBoardService
) -> None:
    mock_boards(respx_mock)
    out = await call(
        "sweep_boards",
        {"companies": ["GitLab"], "title_exclude": [], "remote": ["remote_us", "remote_other"]},
    )
    sources = {j["pay_source"] for j in out["jobs"]}
    assert "structured" in sources  # filled in from the pay-transparency detail call
    assert "parsed_from_text" in sources


@respx.mock(assert_all_called=False)
async def test_sweep_subset_unknown_name_and_cache(
    respx_mock: respx.MockRouter, service: JobBoardService
) -> None:
    mock_boards(respx_mock)
    first = await call("sweep_boards", {"companies": ["linear", "Nobody Inc"]})
    assert [c["company"] for c in first["per_company"]] == ["Linear", "Nobody Inc"]
    nobody = next(c for c in first["per_company"] if c["company"] == "Nobody Inc")
    assert nobody["status"] == "error" and nobody["error"] == "not in watchlist"
    assert first["cache_hits"] == 0
    second = await call("sweep_boards", {"companies": ["Linear"]})
    assert second["cache_hits"] == 1
    assert second["jobs"] == first["jobs"]  # deterministic


@respx.mock(assert_all_called=False)
async def test_sweep_exclusions_and_descriptions(
    respx_mock: respx.MockRouter, service: JobBoardService
) -> None:
    mock_boards(respx_mock)
    base = await call("sweep_boards", {"companies": ["Linear"], "include_description": True})
    assert all(j["description_text"] for j in base["jobs"])
    first = base["jobs"][0]
    second = base["jobs"][1]
    out = await call(
        "sweep_boards",
        {
            "companies": ["Linear"],
            "exclude_ids": [first["id"]],
            "exclude_urls": [second["url"]],
        },
    )
    ids = {j["id"] for j in out["jobs"]}
    assert first["id"] not in ids and second["id"] not in ids
    assert len(out["jobs"]) == len(base["jobs"]) - 2


@respx.mock(assert_all_called=False)
async def test_get_posting_by_url_and_by_ids(
    respx_mock: respx.MockRouter, service: JobBoardService
) -> None:
    mock_boards(respx_mock)
    detail = fixture_json("greenhouse_gitlab_detail.json")
    job = await call("get_posting", {"url": detail["absolute_url"]})
    assert job["company"] == "GitLab"  # resolved from the watchlist
    assert job["pay_source"] == "structured" and job["pay_min"] == 139_200
    assert job["description_text"]

    lever_raw = fixture_json("lever_outreach.json")[0]
    respx_mock.get(LEVER.detail_url("outreach", lever_raw["id"])).mock(
        return_value=httpx.Response(200, json=lever_raw)
    )
    job = await call(
        "get_posting", {"ats": "lever", "token": "outreach", "job_id": lever_raw["id"]}
    )
    assert job["id"] == f"lever:outreach:{lever_raw['id']}"


@respx.mock(assert_all_called=False)
async def test_get_posting_errors(respx_mock: respx.MockRouter, service: JobBoardService) -> None:
    respx_mock.get(url__regex=r"https://api\.lever\.co/.*").mock(
        return_value=httpx.Response(404, text=fixture_text("lever_404.json"))
    )
    with pytest.raises(ToolError, match="not a recognized"):
        await server.mcp.call_tool("get_posting", {"url": "https://www.linkedin.com/jobs/view/1"})
    with pytest.raises(ToolError, match="pass either url"):
        await server.mcp.call_tool("get_posting", {"ats": "lever"})
    with pytest.raises(ToolError, match="404"):
        await server.mcp.call_tool(
            "get_posting", {"ats": "lever", "token": "outreach", "job_id": "x"}
        )


async def test_tools_have_descriptions_and_schemas() -> None:
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert set(tools) == {
        "sweep_boards",
        "get_posting",
        "find_company_posting",
        "resolve_board",
        "is_posting_live",
        "list_watchlist",
        *server.WRITE_TOOLS,
    }
    for tool in tools.values():
        assert tool.description and len(tool.description) > 40
        assert tool.output_schema
        assert tool.annotations is not None
        writes = tool.name in server.WRITE_TOOLS
        assert tool.annotations.read_only_hint is (not writes), tool.name
        if writes:
            assert tool.annotations.destructive_hint is False
        for name, prop in tool.input_schema["properties"].items():
            assert prop.get("description"), f"{tool.name}.{name} has no description"
