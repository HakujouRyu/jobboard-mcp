"""End-to-end M2 tool calls through the MCP server, against fixtures and a temp watchlist."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
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
from jobboard_mcp.service import JobBoardService
from jobboard_mcp.watchlist import WatchlistStore

from .conftest import fixture_json

NOW = datetime(2026, 10, 15, 18, 0, tzinfo=UTC)
GH, ASHBY, LEVER = GreenhouseAdapter(), AshbyAdapter(), LeverAdapter()

WATCHLIST = """\
# Test watchlist

- company: GitLab
  ats: greenhouse
  token: gitlab
  domains: [dev tools, open source]
  tags: [hww]

- company: Temporal Technologies
  ats: ashby
  token: temporal
  domains: [dev tools]
  tags: []

- company: Outreach
  ats: lever
  token: outreach
  domains: [sales tools]
  tags: []

- company: Posit
  careers_url: https://posit.co/careers/
  domains: [open source]
  tags: [hww]
"""
POSIT_PAGE = '<script src="https://boards.greenhouse.io/embed/job_board/js?for=rstudio"></script>'


def mock_apis(router: respx.MockRouter) -> None:
    router.get(GH.list_url("gitlab")).mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_gitlab_list.json"))
    )
    router.get(ASHBY.list_url("temporal")).mock(
        return_value=httpx.Response(200, json=fixture_json("ashby_temporal.json"))
    )
    router.get(LEVER.list_url("outreach")).mock(
        return_value=httpx.Response(200, json=fixture_json("lever_outreach.json"))
    )
    detail = fixture_json("greenhouse_gitlab_detail.json")
    router.get(GH.detail_url("gitlab", str(detail["id"]))).mock(
        return_value=httpx.Response(200, json=detail)
    )
    router.get("https://posit.co/careers/").mock(return_value=httpx.Response(200, text=POSIT_PAGE))
    router.get(GH.list_url("rstudio")).mock(
        return_value=httpx.Response(
            200, json={"jobs": [{**j, "company_name": "Posit"} for j in fixture_json(
                "greenhouse_gitlab_list.json")["jobs"][:3]]}
        )
    )  # fmt: skip
    # Everything else (guessed tokens, unknown jobs) is not found.
    for prefix in (
        "https://boards-api.greenhouse.io/",
        "https://api.ashbyhq.com/",
        "https://api.lever.co/",
        "https://www.workable.com/api/",
        "https://api.rippling.com/",
    ):
        router.get(url__startswith=prefix).mock(return_value=httpx.Response(404, text="Not Found"))


@pytest.fixture
def watchlist_path(tmp_path: Path) -> Path:
    path = tmp_path / "watchlist.yaml"
    path.write_text(WATCHLIST, encoding="utf-8")
    return path


@pytest.fixture
async def service(watchlist_path: Path) -> AsyncIterator[JobBoardService]:
    svc = JobBoardService(
        Settings(), HttpClient(Settings()), store=WatchlistStore(watchlist_path), now=lambda: NOW
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


# ------------------------------------------------------------------ find_company_posting


@respx.mock
async def test_find_on_watched_board(service: JobBoardService) -> None:
    mock_apis(respx.mock)
    out = await call(
        "find_company_posting",
        {"company": "temporal technologies", "title": "Sr. Product Manager - Hybrid Platform"},
    )
    assert out["board_found"] and (out["ats"], out["token"]) == ("ashby", "temporal")
    assert out["company"] == "Temporal Technologies" and out["resolution"] is None
    assert 1 <= len(out["candidates"]) <= 5
    scores = [c["score"] for c in out["candidates"]]
    assert scores == sorted(scores, reverse=True)
    assert all(c["job"]["description_text"] is None for c in out["candidates"])
    top = out["candidates"][0]
    assert top["job"]["title"] == "Senior Product Manager, Hybrid Platform & Deployment Models"
    assert top["score"] >= 80


@respx.mock
async def test_find_matches_a_shorter_name_and_keeps_sweep_fingerprints(
    service: JobBoardService,
) -> None:
    mock_apis(respx.mock)
    title = "Senior Product Manager, Hybrid Platform & Deployment Models"
    out = await call("find_company_posting", {"company": "Temporal", "title": title})
    assert out["company"] == "Temporal Technologies"
    assert out["watchlist_match"] == "loose_name" and out["resolution"] is None
    swept = await call("sweep_boards", {"companies": ["Temporal Technologies"]})
    fingerprints = {j["id"]: j["fingerprint"] for j in swept["jobs"]}
    top = out["candidates"][0]["job"]
    assert top["title"] == title and top["fingerprint"] == fingerprints[top["id"]]


@respx.mock
async def test_find_maps_a_resolved_board_back_to_the_watchlist(service: JobBoardService) -> None:
    mock_apis(respx.mock)
    out = await call("find_company_posting", {"company": "Temporal.io", "title": "Engineer"})
    assert (out["ats"], out["token"]) == ("ashby", "temporal")
    assert out["company"] == "Temporal Technologies" and out["watchlist_match"] == "board"
    assert out["resolution"] is not None
    assert all(c["job"]["company"] == "Temporal Technologies" for c in out["candidates"])


@respx.mock
async def test_find_resolves_careers_page_only_company(service: JobBoardService) -> None:
    mock_apis(respx.mock)
    out = await call("find_company_posting", {"company": "Posit", "title": "Backend Engineer"})
    assert out["board_found"] and (out["ats"], out["token"]) == ("greenhouse", "rstudio")
    assert out["resolution"]["confidence"] == "high"


@respx.mock
async def test_find_without_board(service: JobBoardService) -> None:
    mock_apis(respx.mock)
    out = await call("find_company_posting", {"company": "Nowhere Co", "title": "Engineer"})
    assert out["board_found"] is False and out["candidates"] == []
    assert out["resolution"]["ats"] is None and out["watchlist_match"] is None


# ------------------------------------------------------------------ resolve_board


@respx.mock
async def test_resolve_uses_watchlist_careers_url(service: JobBoardService) -> None:
    mock_apis(respx.mock)
    out = await call("resolve_board", {"company": "Posit"})
    assert (out["ats"], out["token"], out["confidence"]) == ("greenhouse", "rstudio", "high")
    assert out["pages_checked"] == ["https://posit.co/careers/"]


async def test_resolve_needs_input(service: JobBoardService) -> None:
    with pytest.raises(ToolError, match="company and/or careers_url"):
        await server.mcp.call_tool("resolve_board", {})


@respx.mock
async def test_custom_urls_disallowed(watchlist_path: Path) -> None:
    mock_apis(respx.mock)
    settings = Settings(allow_custom_urls=False)
    svc = JobBoardService(settings, store=WatchlistStore(watchlist_path), now=lambda: NOW)
    server.set_service(svc)
    before = watchlist_path.read_text(encoding="utf-8")
    try:
        with pytest.raises(ToolError, match="ALLOW_CUSTOM_URLS"):
            await server.mcp.call_tool(
                "resolve_board", {"company": "Posit", "careers_url": "https://example.com/jobs"}
            )
        with pytest.raises(ToolError, match="ALLOW_CUSTOM_URLS"):
            await server.mcp.call_tool(
                "add_company", {"company": "X", "careers_url": "https://example.com/jobs"}
            )
        with pytest.raises(ToolError, match="ALLOW_CUSTOM_URLS"):
            await server.mcp.call_tool(
                "update_company", {"company": "GitLab", "careers_url": "https://example.com"}
            )
        assert watchlist_path.read_text(encoding="utf-8") == before
        # The watchlist's own careers_url is still used, and clearing one is still allowed.
        out = await call("resolve_board", {"company": "Posit"})
        assert (out["ats"], out["token"]) == ("greenhouse", "rstudio")
        out = await call("find_company_posting", {"company": "Posit", "title": "Engineer"})
        assert out["board_found"]
        out = await call("update_company", {"company": "Posit", "careers_url": ""})
        assert out["entry"]["careers_url"] is None
    finally:
        server.set_service(None)
        await svc.aclose()


def test_allow_custom_urls_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_CUSTOM_URLS", raising=False)
    assert Settings.from_env().allow_custom_urls is True
    monkeypatch.setenv("ALLOW_CUSTOM_URLS", "false")
    assert Settings.from_env().allow_custom_urls is False


# ------------------------------------------------------------------ is_posting_live


@respx.mock
async def test_live_greenhouse_and_lever(service: JobBoardService) -> None:
    mock_apis(respx.mock)
    detail = fixture_json("greenhouse_gitlab_detail.json")
    out = await call("is_posting_live", {"url": detail["absolute_url"]})
    assert out["live"] is True and out["ats"] == "greenhouse" and out["from_cache"] is False
    again = await call("is_posting_live", {"url": detail["absolute_url"]})
    assert again["from_cache"] is True

    gone = await call("is_posting_live", {"ats": "greenhouse", "token": "gitlab", "job_id": "1"})
    assert gone["live"] is False and "404" in gone["reason"]

    lever_gone = await call(
        "is_posting_live",
        {"url": "https://jobs.lever.co/outreach/00000000-0000-0000-0000-000000000000"},
    )
    assert lever_gone["live"] is False


@respx.mock
async def test_live_ashby_uses_board_listing(service: JobBoardService) -> None:
    mock_apis(respx.mock)
    job_id = fixture_json("ashby_temporal.json")["jobs"][0]["id"]
    out = await call(
        "is_posting_live", {"url": f"https://jobs.ashbyhq.com/temporal/{job_id.upper()}"}
    )
    assert out["live"] is True and "listed" in out["reason"]
    missing = await call(
        "is_posting_live",
        {"ats": "ashby", "token": "temporal", "job_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert missing["live"] is False and missing["from_cache"] is True
    no_board = await call("is_posting_live", {"ats": "ashby", "token": "nope", "job_id": "x"})
    assert no_board["live"] is False and "board" in no_board["reason"]


async def test_live_rejects_unknown_url(service: JobBoardService) -> None:
    with pytest.raises(ToolError, match="not a recognized"):
        await server.mcp.call_tool("is_posting_live", {"url": "https://example.com/job/1"})


# ------------------------------------------------------------------ watchlist tools


async def test_list_watchlist_filters(service: JobBoardService, watchlist_path: Path) -> None:
    out = await call("list_watchlist", {})
    assert out["total"] == 4 and out["readonly"] is False
    assert out["path"] == str(watchlist_path)
    assert [
        c["company"] for c in (await call("list_watchlist", {"has_board": False}))["companies"]
    ] == ["Posit"]
    assert {c["company"] for c in (await call("list_watchlist", {"tag": "HWW"}))["companies"]} == {
        "GitLab",
        "Posit",
    }
    assert (await call("list_watchlist", {"domain": "dev"}))["total"] == 2


@respx.mock
async def test_archived_companies_are_skipped_and_hidden(
    service: JobBoardService, watchlist_path: Path
) -> None:
    mock_apis(respx.mock)
    out = await call("update_company", {"company": "outreach", "archived": True})
    assert out["entry"]["archived"] is True and out["board_check"] is None
    assert "  archived: true\n" in watchlist_path.read_text(encoding="utf-8")

    listed = await call("list_watchlist", {})
    assert "Outreach" not in {c["company"] for c in listed["companies"]}
    assert (listed["total"], listed["archived_hidden"]) == (3, 1)
    everything = await call("list_watchlist", {"include_archived": True})
    assert (everything["total"], everything["archived_hidden"]) == (4, 0)

    sweep = await call("sweep_boards", {"title_exclude": []})
    assert "Outreach" not in {c["company"] for c in sweep["per_company"]}
    assert all(j["company"] != "Outreach" for j in sweep["jobs"])
    named = await call("sweep_boards", {"companies": ["Outreach"], "title_exclude": []})
    assert [c["status"] for c in named["per_company"]] == ["ok"] and named["jobs"]

    with pytest.raises(ToolError, match="archived; restore it with update_company"):
        await server.mcp.call_tool("add_company", {"company": "Outreach"})
    out = await call("update_company", {"company": "Outreach", "archived": False})
    assert out["entry"]["archived"] is False
    assert watchlist_path.read_text(encoding="utf-8") == WATCHLIST


@respx.mock
async def test_add_company_validates_board_first(
    service: JobBoardService, watchlist_path: Path
) -> None:
    respx.mock.get(LEVER.list_url("ghost")).mock(
        return_value=httpx.Response(200, json=fixture_json("lever_outreach.json")[:2])
    )
    mock_apis(respx.mock)  # registered after, so "ghost" wins over the catch-all 404s
    before = watchlist_path.read_text(encoding="utf-8")
    with pytest.raises(ToolError, match="failed validation: not_found"):
        await server.mcp.call_tool(
            "add_company", {"company": "Ghost", "ats": "lever", "token": "gh0st"}
        )
    assert watchlist_path.read_text(encoding="utf-8") == before

    out = await call(
        "add_company",
        {"company": "Ghost", "ats": "lever", "token": "ghost", "tags": ["hww"], "domains": ["ai"]},
    )
    assert out["board_check"] == {"status": "ok", "jobs": 2, "error": None}
    text = watchlist_path.read_text(encoding="utf-8")
    assert text.startswith("# Test watchlist") and "\n\n- company: Ghost\n  ats: lever\n" in text


async def test_add_company_without_board_and_duplicates(service: JobBoardService) -> None:
    out = await call("add_company", {"company": "WeMod", "careers_url": "https://wemod.com"})
    assert out["board_check"] is None and out["entry"]["ats"] is None
    with pytest.raises(ToolError, match="already on the watchlist"):
        await server.mcp.call_tool("add_company", {"company": "gitlab"})
    with pytest.raises(ToolError, match="both ats and token"):
        await server.mcp.call_tool("add_company", {"company": "Half", "ats": "lever"})


@respx.mock
async def test_update_company_records_resolved_board(
    service: JobBoardService, watchlist_path: Path
) -> None:
    mock_apis(respx.mock)
    out = await call(
        "update_company", {"company": "posit", "ats": "greenhouse", "token": "rstudio"}
    )
    assert out["board_check"]["status"] == "ok" and out["entry"]["token"] == "rstudio"
    assert "- company: Posit\n  ats: greenhouse\n  token: rstudio\n" in watchlist_path.read_text(
        encoding="utf-8"
    )

    out = await call(
        "update_company", {"company": "Posit", "notes": "Old name RStudio", "tags": []}
    )
    assert out["board_check"] is None and out["entry"]["tags"] == []
    out = await call("update_company", {"company": "Posit", "notes": ""})
    assert out["entry"]["notes"] is None

    out = await call("update_company", {"company": "Posit", "clear_board": True})
    assert out["entry"]["ats"] is None and out["entry"]["token"] is None


@respx.mock
async def test_update_company_rejects_bad_board_and_bad_input(
    service: JobBoardService, watchlist_path: Path
) -> None:
    mock_apis(respx.mock)
    before = watchlist_path.read_text(encoding="utf-8")
    with pytest.raises(ToolError, match="failed validation"):
        await server.mcp.call_tool("update_company", {"company": "GitLab", "token": "gitlab-old"})
    with pytest.raises(ToolError, match="nothing to change"):
        await server.mcp.call_tool("update_company", {"company": "GitLab"})
    with pytest.raises(ToolError, match="not both"):
        await server.mcp.call_tool(
            "update_company", {"company": "GitLab", "clear_board": True, "token": "x"}
        )
    with pytest.raises(ToolError, match="not on the watchlist"):
        await server.mcp.call_tool("update_company", {"company": "Nobody", "notes": "x"})
    assert watchlist_path.read_text(encoding="utf-8") == before


async def test_readonly_store_blocks_writes(watchlist_path: Path) -> None:
    svc = JobBoardService(Settings(), store=WatchlistStore(watchlist_path, readonly=True))
    server.set_service(svc)
    try:
        with pytest.raises(ToolError, match="read-only"):
            await server.mcp.call_tool("add_company", {"company": "X"})
        with pytest.raises(ToolError, match="read-only"):
            await server.mcp.call_tool("update_company", {"company": "GitLab", "notes": "x"})
        assert (await call("list_watchlist", {}))["readonly"] is True
    finally:
        server.set_service(None)
        await svc.aclose()
