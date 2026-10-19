"""The bundled Greenhouse board directory and how resolve_board uses it."""

import httpx
import respx

from jobboard_mcp.directory import Listing, directory, parse_directory
from jobboard_mcp.http import HttpClient
from jobboard_mcp.models import Ats
from jobboard_mcp.resolve import resolve_board

from .test_resolve import gh_url, not_found_everywhere_else

SAMPLE = (
    "# a comment\n"
    "# checked: 2026-08-24\n"
    "Acme Robotics\tacmerobotics2\n"
    "Acme Robotics\tacme-legacy\n"
    "Davidson Kempner Capital Management\tdkcm\n"
    "Davidson Kempner for LinkedIn\tdk-li\n"
    "Globex\tglobex\n"
    "bad line without a tab\n"
)


def test_parse_reads_rows_and_checked_date() -> None:
    d = parse_directory(SAMPLE)
    assert d.checked == "2026-08-24"
    assert d.lookup("Acme Robotics") == [
        Listing("Acme Robotics", "acmerobotics2"),
        Listing("Acme Robotics", "acme-legacy"),
    ]


def test_lookup_ignores_case_and_punctuation() -> None:
    assert [x.token for x in parse_directory(SAMPLE).lookup("acme robotics, ")] == [
        "acmerobotics2",
        "acme-legacy",
    ]


def test_lookup_falls_back_to_one_loose_match() -> None:
    d = parse_directory(SAMPLE)
    assert [x.token for x in d.lookup("Globex Corporation")] == ["globex"]
    assert [x.token for x in d.lookup("Acme")] == ["acmerobotics2", "acme-legacy"]


def test_lookup_never_guesses_between_loose_matches() -> None:
    assert parse_directory(SAMPLE).lookup("Davidson Kempner") == []


def test_lookup_unknown_company() -> None:
    assert parse_directory(SAMPLE).lookup("Initech") == []
    assert parse_directory(SAMPLE).lookup("") == []


def test_bundled_directory_loads() -> None:
    d = directory()
    assert len(d.names) > 4000
    assert d.checked == "2026-08-24"
    assert [x.token for x in d.lookup("Addepar")] == ["addepar1"]


@respx.mock
async def test_resolve_finds_a_listed_board_the_name_doesnt_give(http: HttpClient) -> None:
    respx.get(gh_url("addepar1")).mock(
        return_value=httpx.Response(200, json={"jobs": [{"company_name": "Addepar"}] * 3})
    )
    not_found_everywhere_else(respx.mock)
    r = await resolve_board(http, "Addepar")
    assert (r.ats, r.token, r.jobs) == (Ats.GREENHOUSE, "addepar1", 3)
    assert any("listed in the board directory as 'Addepar'" in e for e in r.evidence)


@respx.mock
async def test_resolve_ignores_a_listed_board_that_is_gone(http: HttpClient) -> None:
    not_found_everywhere_else(respx.mock)
    r = await resolve_board(http, "Addepar")
    assert r.ats is None
    assert "addepar1" in r.evidence[0]
