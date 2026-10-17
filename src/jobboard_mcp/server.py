"""MCP server: tool definitions and transport selection."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import AfterValidator, Field

from jobboard_mcp import __version__
from jobboard_mcp.config import (
    DEFAULT_WATCHLIST,
    EXAMPLE_WATCHLIST,
    Settings,
    configure_logging,
)
from jobboard_mcp.filters import SweepFilters, check_term
from jobboard_mcp.http import FetchError
from jobboard_mcp.models import (
    Ats,
    FindResult,
    Job,
    LiveResult,
    Remote,
    Resolution,
    SweepResult,
    WatchlistChange,
    WatchlistEntry,
    WatchlistView,
)
from jobboard_mcp.service import JobBoardService
from jobboard_mcp.watchlist import WatchlistError, seed_watchlist

INSTRUCTIONS = """\
Searches public Greenhouse, Ashby, and Lever job boards for a watchlist of companies and returns
normalized jobs. sweep_boards: filtered sweep of every watched board. Its filters are off
unless passed, so pass the user's title, remote, pay, and date preferences on every call.
get_posting: one full
posting. find_company_posting: find a role seen elsewhere on the company's own board.
resolve_board: work out a company's board. is_posting_live: check a posting is still open.
list_watchlist / add_company / update_company: manage the watchlist (the only writes). To
drop a company, archive it with update_company(archived=true); there is no delete.
list_watchlist's path is the watchlist file, if the user wants to edit it by hand.
Public data only. Remote status is never guessed: 'unknown' jobs are kept and flagged.
pay_min / pay_max are annual amounts in pay_currency (USD when the posting offers it), never
converted; pay_other_currencies lists any other currencies posted, and pay_posted_interval says
how the pay was posted. Each job's flags list missing data: pay_unknown, date_unknown,
remote_unknown. Filters keep jobs they can't check, so check a job's unverified_filters
before treating it as meeting the user's criteria (unverified_counts totals them).
Title terms are plain phrases, never regexes; term_hits shows how many titles each matched.
US-focused: remote_us is the only country-level remote status.
Hosted servers may refuse careers_url arguments; resolve_board then uses the watchlist's.
"""

# A job is about 220 tokens without its description (measured on the test fixtures), so the
# max keeps one sweep's output near 55k tokens.
SWEEP_LIMIT_DEFAULT = 100
SWEEP_LIMIT_MAX = 250

# Checked when the call comes in, so a regex-looking term fails before any board is fetched.
TitleTerm = Annotated[str, AfterValidator(check_term)]

_service: JobBoardService | None = None


def get_service() -> JobBoardService:
    global _service
    if _service is None:
        _service = JobBoardService(Settings.from_env())
    return _service


def set_service(service: JobBoardService | None) -> None:
    """Swap the service (tests)."""
    global _service
    _service = service


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncGenerator[None]:
    try:
        yield
    finally:
        if _service is not None:
            await _service.aclose()


mcp = MCPServer(
    "jobboard-mcp",
    version=__version__,
    instructions=INSTRUCTIONS,
    lifespan=lifespan,
)

_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)


@mcp.tool(annotations=_READ_ONLY)
async def sweep_boards(
    companies: Annotated[
        list[str] | None,
        Field(
            description=(
                "Watchlist company names to sweep, archived ones included. "
                "Default: every company that isn't archived."
            )
        ),
    ] = None,
    title_include: Annotated[
        list[TitleTerm] | None,
        Field(
            description=(
                "Keep titles matching any term. Plain terms match whole words, case-insensitively,"
                " plus a plural ('engineer' matches 'Engineers', not 'Engineering'). Spaces and"
                " hyphens are interchangeable. Pass each spelling titles use: 'senior' and 'sr',"
                " 'full stack' and 'fullstack', 'engineer' and 'engineering' if wanted. Not"
                " regexes: a term starting with 're:' or using | * ? ( ) [ ] { } ^ $ \\ is"
                " refused. term_hits in the result says how many titles each term matched."
            )
        ),
    ] = None,
    title_exclude: Annotated[
        list[TitleTerm] | None,
        Field(
            description=(
                "Drop titles matching any term (same syntax), such as ['intern', 'internship',"
                " 'new grad']. Default: none."
            )
        ),
    ] = None,
    remote: Annotated[
        list[Remote] | None,
        Field(description="Remote statuses to keep. Default: every status."),
    ] = None,
    include_unknown_remote: Annotated[
        bool,
        Field(description="Keep jobs whose remote status is unknown (flagged remote_unknown)."),
    ] = True,
    min_pay: Annotated[
        int | None,
        Field(
            description=(
                "Annual amount in min_pay_currency. Drops jobs whose posted maximum in that"
                " currency is below this. Jobs with no pay in that currency are kept."
            ),
            ge=0,
        ),
    ] = None,
    min_pay_currency: Annotated[
        str,
        Field(
            description="ISO 4217 code that min_pay is in. Nothing is converted.",
            pattern=r"^[A-Za-z]{3}$",
        ),
    ] = "USD",
    posted_within_days: Annotated[
        int | None,
        Field(
            description="Only jobs posted in the last N days. Unknown dates are kept and flagged.",
            ge=0,
        ),
    ] = None,
    exclude_ids: Annotated[
        list[str] | None, Field(description="Job ids already seen ({ats}:{token}:{id}).")
    ] = None,
    exclude_fingerprints: Annotated[
        list[str] | None, Field(description="Fingerprints already seen.")
    ] = None,
    exclude_urls: Annotated[
        list[str] | None, Field(description="Posting URLs already seen.")
    ] = None,
    include_description: Annotated[
        bool, Field(description="Include plain-text descriptions (much larger output).")
    ] = False,
    refresh: Annotated[bool, Field(description="Bypass the 3-hour cache.")] = False,
    limit: Annotated[
        int,
        Field(
            description=(
                "Most jobs to return, newest first. If the result is truncated, narrow the"
                " filters or pass the returned ids in exclude_ids to get the next page."
            ),
            ge=1,
            le=SWEEP_LIMIT_MAX,
        ),
    ] = SWEEP_LIMIT_DEFAULT,
) -> SweepResult:
    """Sweep every watched job board (or a named subset) in parallel and return matching jobs,
    newest first, with per-company status. One company failing never fails the sweep."""
    service = get_service()
    filters = SweepFilters(
        today=service.today(),
        title_include=title_include or (),
        title_exclude=title_exclude or (),
        remote=remote or (),
        include_unknown_remote=include_unknown_remote,
        min_pay=min_pay,
        min_pay_currency=min_pay_currency.upper(),
        posted_within_days=posted_within_days,
        exclude_ids=exclude_ids or (),
        exclude_fingerprints=exclude_fingerprints or (),
        exclude_urls=exclude_urls or (),
    )
    try:
        return await service.sweep(
            filters,
            companies=companies,
            include_description=include_description,
            refresh=refresh,
            limit=limit,
        )
    except WatchlistError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=_READ_ONLY)
async def get_posting(
    url: Annotated[
        str | None,
        Field(description="A Greenhouse, Ashby, or Lever posting URL. Or pass ats, token, job_id."),
    ] = None,
    ats: Annotated[Ats | None, Field(description="greenhouse, ashby, or lever.")] = None,
    token: Annotated[str | None, Field(description="Board token, e.g. 'gitlab'.")] = None,
    job_id: Annotated[str | None, Field(description="The ATS's own job id.")] = None,
    refresh: Annotated[bool, Field(description="Bypass the cache.")] = False,
) -> Job:
    """Fetch one posting with its full plain-text description and the best available pay data
    (Greenhouse pay-transparency ranges included)."""
    try:
        return await get_service().get_posting(
            ats=ats, token=token, job_id=job_id, url=url, refresh=refresh
        )
    except (ValueError, FetchError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=_READ_ONLY)
async def find_company_posting(
    company: Annotated[str, Field(description="Company name, e.g. as shown on LinkedIn.")],
    title: Annotated[str, Field(description="Job title as seen elsewhere.")],
    location: Annotated[
        str | None, Field(description="Optional location as seen elsewhere; breaks ties.")
    ] = None,
    refresh: Annotated[bool, Field(description="Bypass the cache.")] = False,
) -> FindResult:
    """Find a role seen on LinkedIn, Indeed, etc. on the company's own job board. Uses the
    watchlist board if known, otherwise resolves it. Returns up to 5 candidates scored 0-100
    (titles normalized: Sr./Senior, SWE, levels; seniority or role mismatches score lower)."""
    try:
        return await get_service().find_company_posting(company, title, location, refresh=refresh)
    except (ValueError, FetchError, WatchlistError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=_READ_ONLY)
async def resolve_board(
    company: Annotated[str | None, Field(description="Company name.")] = None,
    careers_url: Annotated[
        str | None,
        Field(
            description=(
                "Careers page. Default: the watchlist's careers_url for the company. "
                "Refused if the server disallows custom URLs."
            )
        ),
    ] = None,
    refresh: Annotated[bool, Field(description="Bypass the cache.")] = False,
) -> Resolution:
    """Work out which Greenhouse, Ashby, or Lever board a company uses: probes likely tokens and
    scans the careers page (plus up to two same-site careers links) for board links. Read-only:
    record a result with update_company."""
    try:
        return await get_service().resolve_board(company, careers_url, refresh=refresh)
    except (ValueError, WatchlistError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=_READ_ONLY)
async def is_posting_live(
    url: Annotated[
        str | None,
        Field(description="A Greenhouse, Ashby, or Lever posting URL. Or pass ats, token, job_id."),
    ] = None,
    ats: Annotated[Ats | None, Field(description="greenhouse, ashby, or lever.")] = None,
    token: Annotated[str | None, Field(description="Board token.")] = None,
    job_id: Annotated[str | None, Field(description="The ATS's own job id.")] = None,
    refresh: Annotated[
        bool, Field(description="Check now instead of trusting a cached answer (up to 3 h old).")
    ] = False,
) -> LiveResult:
    """Check whether a posting is still open, via the ATS API rather than the page. Ashby has no
    per-job endpoint, so its answer comes from the (usually cached) board listing."""
    try:
        return await get_service().is_posting_live(
            url=url, ats=ats, token=token, job_id=job_id, refresh=refresh
        )
    except (ValueError, FetchError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=_READ_ONLY)
async def list_watchlist(
    has_board: Annotated[
        bool | None,
        Field(description="true: only companies with a known board; false: careers page only."),
    ] = None,
    tag: Annotated[
        str | None, Field(description="Only companies with this tag, e.g. 'hww'.")
    ] = None,
    domain: Annotated[
        str | None, Field(description="Only companies whose domains contain this text.")
    ] = None,
    include_archived: Annotated[
        bool, Field(description="Also list archived companies (hidden by default).")
    ] = False,
) -> WatchlistView:
    """List watched companies with their boards, domains, tags, and interview notes."""
    try:
        return get_service().list_watchlist(
            has_board=has_board, tag=tag, domain=domain, include_archived=include_archived
        )
    except WatchlistError as exc:
        raise ToolError(str(exc)) from exc


_WRITES = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)


@mcp.tool(annotations=_WRITES)
async def add_company(
    company: Annotated[str, Field(description="Display name.")],
    ats: Annotated[
        Ats | None, Field(description="greenhouse, ashby, or lever; omit if unknown.")
    ] = None,
    token: Annotated[str | None, Field(description="Board token; required with ats.")] = None,
    careers_url: Annotated[str | None, Field(description="Careers page URL.")] = None,
    domains: Annotated[list[str] | None, Field(description="e.g. ['dev tools'].")] = None,
    tags: Annotated[list[str] | None, Field(description="e.g. ['hww'].")] = None,
    interview_note: Annotated[str | None, Field(description="Interview process notes.")] = None,
    notes: Annotated[str | None, Field(description="Anything else.")] = None,
) -> WatchlistChange:
    """Add a company to the server's watchlist. A board, if given, is validated with a live fetch
    first; nothing is saved if that fails. Writes only the watchlist file."""
    entry = WatchlistEntry(
        company=company.strip(),
        ats=ats,
        token=token,
        careers_url=careers_url,
        domains=domains or [],
        tags=tags or [],
        interview_note=interview_note,
        notes=notes,
    )
    try:
        return await get_service().add_company(entry)
    except WatchlistError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=_WRITES)
async def update_company(
    company: Annotated[str, Field(description="Current name on the watchlist.")],
    new_name: Annotated[str | None, Field(description="Rename the company.")] = None,
    ats: Annotated[Ats | None, Field(description="Set the ATS (with token).")] = None,
    token: Annotated[str | None, Field(description="Set the board token.")] = None,
    clear_board: Annotated[bool, Field(description="Remove the ats and token.")] = False,
    careers_url: Annotated[str | None, Field(description="New careers URL; '' clears.")] = None,
    domains: Annotated[list[str] | None, Field(description="Replace domains; [] clears.")] = None,
    tags: Annotated[list[str] | None, Field(description="Replace tags; [] clears.")] = None,
    interview_note: Annotated[str | None, Field(description="New note; '' clears.")] = None,
    notes: Annotated[str | None, Field(description="New notes; '' clears.")] = None,
    archived: Annotated[
        bool | None,
        Field(
            description=(
                "true archives the company: sweeps skip it and list_watchlist hides it, but "
                "its notes are kept. false restores it."
            )
        ),
    ] = None,
) -> WatchlistChange:
    """Change a watchlist entry. Only the fields you pass change. Archive a company you no longer
    want swept with archived=true. A new board is validated with a live fetch first; nothing is
    saved if that fails. Writes only the watchlist file."""
    changes: dict[str, object] = {}
    if new_name is not None:
        changes["company"] = new_name.strip()
    if clear_board:
        if ats is not None or token is not None:
            raise ToolError("pass clear_board or ats/token, not both")
        changes["ats"] = changes["token"] = None
    if ats is not None:
        changes["ats"] = ats
    if token is not None:
        changes["token"] = token
    for key, value in (
        ("careers_url", careers_url),
        ("interview_note", interview_note),
        ("notes", notes),
    ):
        if value is not None:
            changes[key] = value or None
    if domains is not None:
        changes["domains"] = domains
    if tags is not None:
        changes["tags"] = tags
    if archived is not None:
        changes["archived"] = archived
    if not changes:
        raise ToolError("nothing to change")
    try:
        return await get_service().update_company(company, changes)
    except WatchlistError as exc:
        raise ToolError(str(exc)) from exc


WRITE_TOOLS = ("add_company", "update_company")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jobboard-mcp", description=INSTRUCTIONS)
    parser.add_argument("--http", action="store_true", help="streamable HTTP (not until M3)")
    args = parser.parse_args(argv)
    if args.http or os.environ.get("MCP_TRANSPORT", "stdio").lower() not in ("", "stdio"):
        sys.exit("jobboard-mcp: only the stdio transport is available in this version")
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    # Only the default path is seeded: a missing WATCHLIST_PATH is more likely a typo.
    if settings.watchlist_path == DEFAULT_WATCHLIST and seed_watchlist(
        DEFAULT_WATCHLIST, EXAMPLE_WATCHLIST
    ):
        logging.getLogger("jobboard_mcp.server").info(
            "copied the example watchlist", extra={"fields": {"path": str(DEFAULT_WATCHLIST)}}
        )
    if settings.watchlist_readonly:
        for name in WRITE_TOOLS:
            mcp.remove_tool(name)
    set_service(JobBoardService(settings))
    mcp.run("stdio")


if __name__ == "__main__":
    main()
