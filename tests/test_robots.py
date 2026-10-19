"""robots.txt parsing and matching (RFC 9309)."""

import time

import pytest

from jobboard_mcp.robots import MAX_BYTES, parse_robots

PRODUCT = "jobboard-mcp"


def rules(text: str):
    return parse_robots(text.encode(), PRODUCT)


def test_empty_file_allows_everything() -> None:
    assert rules("").allows("/careers")
    assert rules("# nothing here\n").allows("/careers")


def test_no_matching_group_allows_everything() -> None:
    assert rules("User-agent: Googlebot\nDisallow: /\n").allows("/careers")


def test_star_group_applies_when_ours_is_missing() -> None:
    r = rules("User-agent: *\nDisallow: /private\n")
    assert not r.allows("/private/jobs")
    assert r.allows("/careers")


def test_our_group_replaces_the_star_group() -> None:
    r = rules("User-agent: *\nDisallow: /\n\nUser-agent: jobboard-mcp\nDisallow: /admin\n")
    assert r.allows("/careers")
    assert not r.allows("/admin")


@pytest.mark.parametrize("agent", ["jobboard-mcp", "JobBoard-MCP", "jobboard-mcp/0.1"])
def test_agent_matches_case_insensitively_on_the_product_token(agent: str) -> None:
    assert not rules(f"User-agent: {agent}\nDisallow: /\n").allows("/careers")


def test_similar_agent_names_dont_match() -> None:
    assert rules("User-agent: jobboard\nDisallow: /\n").allows("/careers")


def test_groups_for_the_same_agent_are_merged() -> None:
    text = (
        "User-agent: jobboard-mcp\nDisallow: /a\n\n"
        "User-agent: other\nDisallow: /b\n\n"
        "User-agent: jobboard-mcp\nDisallow: /c\n"
    )
    r = rules(text)
    assert not r.allows("/a") and not r.allows("/c")
    assert r.allows("/b")


def test_consecutive_agent_lines_share_one_group() -> None:
    r = rules("User-agent: other\nUser-agent: jobboard-mcp\nDisallow: /jobs\n")
    assert not r.allows("/jobs")


def test_longest_match_wins_whatever_the_order() -> None:
    r = rules("User-agent: *\nAllow: /careers/open\nDisallow: /careers\n")
    assert r.allows("/careers/open/123")
    assert not r.allows("/careers/closed")
    r = rules("User-agent: *\nDisallow: /careers/apply\nAllow: /careers\n")
    assert r.allows("/careers")
    assert not r.allows("/careers/apply/1")


def test_allow_wins_a_tie() -> None:
    assert rules("User-agent: *\nDisallow: /jobs\nAllow: /jobs\n").allows("/jobs")


def test_empty_disallow_allows_everything() -> None:
    assert rules("User-agent: *\nDisallow:\n").allows("/careers")


def test_rules_before_any_agent_are_ignored() -> None:
    assert rules("Disallow: /\nUser-agent: *\nAllow: /x\n").allows("/careers")


def test_wildcards_and_end_anchor() -> None:
    r = rules("User-agent: *\nDisallow: /*.pdf$\nDisallow: /search*q=\n")
    assert not r.allows("/files/handbook.pdf")
    assert r.allows("/files/handbook.pdf?v=2")
    assert not r.allows("/search?page=1&q=engineer")
    assert r.allows("/search?page=1")


def test_end_anchor_without_wildcard_is_exact() -> None:
    r = rules("User-agent: *\nDisallow: /jobs$\n")
    assert not r.allows("/jobs")
    assert r.allows("/jobs/123")


def test_query_is_part_of_the_path() -> None:
    r = rules("User-agent: *\nDisallow: /jobs?dept=\n")
    assert not r.allows("/jobs?dept=eng")
    assert r.allows("/jobs")


def test_percent_encoding_is_normalized() -> None:
    r = rules("User-agent: *\nDisallow: /%7Ejobs\n")
    assert not r.allows("/~jobs")
    assert not r.allows("/%7ejobs")


def test_pattern_without_leading_slash_is_treated_as_rooted() -> None:
    assert not rules("User-agent: *\nDisallow: careers\n").allows("/careers")


def test_comments_crlf_and_bom_are_handled() -> None:
    text = "\N{ZERO WIDTH NO-BREAK SPACE}User-agent: * # everyone\r\nDisallow: /x # no\r\n"
    assert not parse_robots(text.encode(), PRODUCT).allows("/x")


def test_unknown_keys_are_ignored() -> None:
    r = rules("User-agent: *\nCrawl-delay: 10\nSitemap: https://co.test/s.xml\nDisallow: /x\n")
    assert not r.allows("/x")


def test_only_the_first_max_bytes_are_read() -> None:
    padding = "#" * MAX_BYTES + "\n"
    assert rules("User-agent: *\n" + padding + "Disallow: /\n").allows("/careers")


def test_hostile_file_matches_quickly() -> None:
    pattern = "/" + "a*" * 2000 + "b$"
    r = rules(f"User-agent: *\nDisallow: {pattern}\n")
    path = "/" + "a" * 50_000
    started = time.perf_counter()
    assert r.allows(path)
    assert time.perf_counter() - started < 1.0
