"""MCP server: tool definitions and transport selection."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from jobboard_mcp import __version__
from jobboard_mcp.config import Settings, configure_logging
from jobboard_mcp.filters import DEFAULT_REMOTE, DEFAULT_TITLE_EXCLUDE, SweepFilters
from jobboard_mcp.http import FetchError
from jobboard_mcp.models import Ats, Job, Remote, SweepResult
from jobboard_mcp.service import JobBoardService
from jobboard_mcp.watchlist import WatchlistError

INSTRUCTIONS = """\
Searches public Greenhouse, Ashby, and Lever job boards for a watchlist of companies and returns
normalized jobs. Use sweep_boards for a filtered sweep and get_posting for one full posting.
Read-only; public data only. Remote status is never guessed: 'unknown' jobs are kept and flagged.
"""

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
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
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
        Field(description="Watchlist company names to sweep. Default: the whole watchlist."),
    ] = None,
    title_include: Annotated[
        list[str] | None,
        Field(
            description=(
                "Keep titles matching any term. Plain terms match whole words, case-insensitively,"
                " plus a plural ('engineer' matches 'Engineers', not 'Engineering'). Prefix with"
                " 're:' for a regex."
            )
        ),
    ] = None,
    title_exclude: Annotated[
        list[str] | None,
        Field(
            description=(
                "Drop titles matching any term (same syntax). Default: "
                + ", ".join(DEFAULT_TITLE_EXCLUDE)
                + ". Pass [] to exclude nothing."
            )
        ),
    ] = None,
    remote: Annotated[
        list[Remote] | None,
        Field(description="Remote statuses to keep. Default: remote_us, remote_global."),
    ] = None,
    include_unknown_remote: Annotated[
        bool,
        Field(description="Keep jobs whose remote status is unknown (flagged remote_unknown)."),
    ] = True,
    min_pay: Annotated[
        int | None,
        Field(
            description=(
                "Annual USD. Drops jobs whose posted maximum is below this. Jobs with no USD pay"
                " are kept and flagged pay_unknown."
            ),
            ge=0,
        ),
    ] = None,
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
) -> SweepResult:
    """Sweep every watched job board (or a named subset) in parallel and return matching jobs,
    newest first, with per-company status. One company failing never fails the sweep."""
    service = get_service()
    filters = SweepFilters(
        today=service.today(),
        title_include=title_include or (),
        title_exclude=DEFAULT_TITLE_EXCLUDE if title_exclude is None else title_exclude,
        remote=DEFAULT_REMOTE if remote is None else remote,
        include_unknown_remote=include_unknown_remote,
        min_pay=min_pay,
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jobboard-mcp", description=INSTRUCTIONS)
    parser.add_argument("--http", action="store_true", help="streamable HTTP (not until M3)")
    args = parser.parse_args(argv)
    if args.http or os.environ.get("MCP_TRANSPORT", "stdio").lower() not in ("", "stdio"):
        sys.exit("jobboard-mcp: only the stdio transport is available in this version")
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    set_service(JobBoardService(settings))
    mcp.run("stdio")


if __name__ == "__main__":
    main()
