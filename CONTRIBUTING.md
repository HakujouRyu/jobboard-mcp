# Contributing

Thanks for helping out. This guide covers setting up, the rules the code depends on, how the
tests work, and step-by-step recipes for the two most common changes: supporting another ATS and
adding a tool.

Read [docs/architecture.md](docs/architecture.md) first. It's short and has diagrams.
[SPEC.md](SPEC.md) is the original design and milestone plan.
If you work with an AI coding agent, [AGENTS.md](AGENTS.md) (which `CLAUDE.md` imports) gives
it the commands, the rules it most often breaks, and which docs to update.

## Setup

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.12 if you don't have it.

```sh
git clone https://github.com/HakujouRyu/jobboard-mcp
cd jobboard-mcp
uv sync
uv run pre-commit install
```

The pre-commit hooks run ruff (lint with fixes, then format), check that `uv.lock` matches
`pyproject.toml`, audit the GitHub workflows with [zizmor](https://docs.zizmor.sh/), and fix
whitespace and line endings. `uv run pre-commit run --all-files` runs
them by hand.

## Checks

All three must pass before a change is merged:

```sh
uv run pytest                       # offline, against fixtures (~5 s)
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

`uv run ruff format .` fixes formatting. The live tests are opt-in:

```sh
uv run pytest -m live               # hits the real board APIs
```

Run them when you change an adapter, `http.py`, or `resolve.py`. They depend on real boards,
which change, so a live failure can mean a board changed rather than a bug. Look at which one
failed before you change any code.

### CI

GitHub Actions runs the three checks above on every PR and every push to `main`
([ci.yml](.github/workflows/ci.yml)). The tests run on Linux, Windows, and macOS, each with
Python 3.12 (the oldest supported) and 3.14 (the newest). Lint and type checks run once, and so
does zizmor, which audits the workflows themselves. Actions are pinned to commit SHAs, and
Dependabot keeps the pins current. The
`checks` job passes only when all of those do, and it's the one a merge waits on.

[live.yml](.github/workflows/live.yml) runs the live tests on every push to `main` and every
Monday, since boards change without any commit here. A failure opens an issue labeled
`live-tests`, or comments on the one already open, and the next passing run closes it. GitHub
turns off scheduled workflows after 60 days without a commit, so on a quiet repo, re-enable it
from the Actions tab or run it by hand.

## Ground rules

These are what make the server trustworthy, and review checks for them.

1. **Public JSON APIs only.** Don't scrape LinkedIn, Indeed, Glassdoor, or any site whose terms
   forbid it. The only HTML the server reads is careers pages, in `resolve_board`, and only to
   find board links. Don't extract job content from HTML. Every careers-page request goes
   through `get_text`, which keeps it on public addresses (`urlguard.py`). Don't fetch pages
   any other way.
2. **Read-only, except the watchlist.** The server writes nothing except the watchlist file,
   and only through `add_company` / `update_company`. Any new write tool must be removed when
   `WATCHLIST_READONLY=true` (add it to `WRITE_TOOLS` in `server.py`).
3. **Never guess `remote_us`.** It needs positive evidence. When in doubt, return `unknown`.
   The same goes for pay: no currency conversion, and no annual figure unless the posting gives
   one or an interval you can annualize.
4. **Flag, don't drop.** If a filter can't be checked because data is missing, keep the job and
   flag it.
5. **One company never fails a sweep.** Errors from a single board become its `per_company`
   status.
6. **Deterministic output.** The same boards and filters give the same output, in the same
   order. Sort with a full tiebreak.
7. **Nothing on stdout.** stdout is the MCP transport. Log through `logging` (configured to
   write JSON to stderr) and never `print()`.
8. **Keep the pure modules pure.** `normalize.py`, `filters.py`, `matching.py`, and `models.py`
   don't do I/O and don't read the clock. Pass "today" in.
9. **Patterns stay fast on any input.** Board text and client arguments can be any size and
   shape, and matching runs on the event loop, so one slow match stalls every request. Don't
   nest quantifiers (`(a+)+`) or repeat alternatives that overlap (`(a|ab)*`), and never compile
   a regex from client input: escape it. Test new patterns on long, hostile input, not just
   real postings.

## Where things go

| Change | Where |
|--------|-------|
| A new tool, or a change to a tool's arguments or description | `server.py` (schema only) and `service.py` (logic) |
| How a field is parsed for every ATS | `normalize.py` |
| How one ATS's JSON maps to a `Job` | `adapters/<ats>.py` |
| Filtering, sorting | `filters.py` |
| Request behavior: cache, retries, limits | `http.py` |
| Which addresses careers-page fetches may reach | `urlguard.py` |
| New settings | `config.py`, plus the table in the README |
| New result fields | `models.py`, with a `Field(description=...)` if the name isn't self-explanatory |

## Tests

```
tests/
  conftest.py        fixture_json(), fixture_text(), and an `http` fixture with retries that don't sleep
  fixtures/          trimmed real API responses (see docs/api-notes.md for where each came from)
  test_normalize.py  remote, pay, date, and HTML rules
  test_filters.py    filter stages, flags, sorting
  test_matching.py   title scoring
  test_adapters.py   each adapter against its fixtures, including empty boards and 404s
  test_http.py       cache, revalidation, retries, concurrency caps
  test_urlguard.py   careers-page SSRF guard: address checks, redirects, pinning
  test_resolve.py    token candidates, link scanning, ranking
  test_watchlist.py  validation and comment-preserving edits
  test_tools.py      end-to-end tool calls through the MCP server (M1 tools)
  test_tools_m2.py   the same for the M2 tools
  test_live.py       opt-in checks against the real APIs (marked `live`)
```

Patterns to follow:

- **Pure functions:** call them directly with literal inputs. When a bug comes from a real
  posting, use the posting's exact string as the test input.
- **HTTP:** mock with [respx](https://lundberg.github.io/respx/). Build URLs from the adapter
  (`GreenhouseAdapter().list_url("gitlab")`) instead of writing them out by hand.
- **Tool tests:** build a `JobBoardService` with a fixed `now` and an in-memory `watchlist`, set
  it with `server.set_service(...)`, and call `server.mcp.call_tool(name, args)`. See the
  `service` fixture and `call()` helper in [tests/test_tools.py](tests/test_tools.py).
- **Watchlist writes:** use a copy in `tmp_path`, never your own watchlist.
- **The example watchlist:** the live tests sweep `src/jobboard_mcp/watchlist.example.yaml`,
  not your own list. Keep at least one board per ATS in it.
- `asyncio_mode = "auto"` is on, so async tests need no decorator.
- **DNS:** an autouse fixture in `conftest.py` makes every host resolve to one public address
  and turns off address pinning, so offline tests never do DNS and respx routes match on
  hostnames. `test_urlguard.py` turns pinning back on where it tests it.

### Recording a fixture

1. Fetch the real response, such as
   `curl -s 'https://api.lever.co/v0/postings/outreach?mode=json' > raw.json`.
   (On Windows, curl can fail TLS revocation checks against Ashby. `uv run python -c` with httpx
   works.)
2. Trim it to a handful of **whole** job objects that cover the cases you care about. Don't
   edit field values, since the point of a fixture is that it's real.
3. Save it as `tests/fixtures/<ats>_<board>.json`, and add a row to the fixture table in
   [docs/api-notes.md](docs/api-notes.md) saying where it came from and what it covers.
4. If the live response doesn't match the docs or the code's assumptions, write that down in
   api-notes.md too.

## Recipe: supporting another ATS

Workable, SmartRecruiters, and Recruitee are the candidates in SPEC.md §3.

1. **Check the live API first.** Fetch two or three real boards, including an empty one and an
   unknown token. Record the differences from the vendor docs in `docs/api-notes.md`.
2. **Record fixtures** as above, including the 404 response.
3. **Add the enum value** to `Ats` in [models.py](src/jobboard_mcp/models.py).
4. **Write `adapters/<ats>.py`** implementing the `Adapter` protocol in
   [adapters/base.py](src/jobboard_mcp/adapters/base.py):
   - `list_url(token)`, and `list_jobs(...)`, which wraps parsing in `ListingMemo`
   - `to_job(raw, company, token)` using `build_job(...)`, then `job.set_pay(...)`. Feed
     `classify_remote` the structured workplace field (lowercased) when the ATS has one, and
     `None` when it doesn't. Feed it structured countries if there are any.
   - `get_job(...)`, which raises `NotFound` if the job is gone
   - `check_live(...)`, using the API and never the posting page
   - `needs_detail_for_pay(job)`: `True` only if the detail endpoint has pay that the list
     doesn't
   - `parse_url(url)`, for every public posting URL form
5. **Register it** in `ADAPTERS` in
   [adapters/\_\_init\_\_.py](src/jobboard_mcp/adapters/__init__.py).
6. **Teach `resolve.py`** the board URL patterns (`_BOARD_PATTERNS`), and remove the host from
   `_OTHER_ATS` if it's listed there.
7. **Tests:** adapter tests (list, detail, pay, remote, empty board, 404, URL parsing), a
   board for the new ATS in the `BOARDS` table in `test_tools.py`, and a live test.
8. **Docs:** the README's supported-boards line, the URL table in `docs/tools.md`, and
   `docs/api-notes.md`.

## Recipe: adding a tool

1. Write the logic as a `JobBoardService` method in `service.py`. Return a Pydantic model from
   `models.py`.
2. Add a thin wrapper in `server.py`:
   - Declare each argument with `Annotated[..., Field(description=...)]`. The model reads these
     descriptions, so make them say what the argument does and what its default is.
   - Write a docstring of one or two sentences. It becomes the tool's description.
   - Use `annotations=_READ_ONLY`, or `_WRITES` and an entry in `WRITE_TOOLS`.
   - Turn expected errors (`ValueError`, `FetchError`, `WatchlistError`) into `ToolError`.
3. Mention it in `INSTRUCTIONS` at the top of `server.py`.
4. Add a test that goes through `server.mcp.call_tool`, and a live test if the tool makes
   requests.
5. Document it in `docs/tools.md` and the README's tool table.

## Debugging

- **MCP Inspector:** `npx @modelcontextprotocol/inspector uv run jobboard-mcp` opens a UI for
  listing tools and calling them by hand.
- **Logs:** set `LOG_LEVEL=DEBUG` to log every request with its status and latency. Logs go to
  stderr as one JSON object per line. Each board fetch logs `company`, `ats`, `status`,
  `latency_ms`, `cache_hit`, and `jobs`.
- **Claude desktop app:** the app writes server stderr to its MCP log files (`mcp-server-jobboard.log`
  in the app's logs folder).

## Style

- `ruff` (rules E, F, W, I, UP, B, SIM, RUF; 100-character lines) and `pyright` in standard
  mode. Type everything, and start modules that hold code with
  `from __future__ import annotations`.
- Every module starts with a docstring saying what it's for.
- Keep look-alike characters out of source. When code must match one, such as the en dash
  (U+2013) that real postings put in pay ranges (`$150K\u2013$180K`), write it as an
  escape: `\u2013`. It's easy to type a hyphen where an en dash was meant, or the
  reverse, and the two look the same in an editor. Ruff's RUF001-RUF003 rules enforce this.
  Test fixtures are real API data and keep their characters.
- Comments explain *why*, especially when the code handles a quirk of a real API. Point to
  `docs/api-notes.md` when that's where the evidence is.
- Prefer small pure functions and frozen dataclasses. Jobs from `list_jobs` are shared through
  the memo, so never change one in place. Use `job.model_copy(update=...)`.

## Commits and pull requests

- PRs are squash-merged, so each PR lands as one commit on `main`. Keep a PR to one thing; split
  unrelated changes into separate PRs. Commits inside a PR can be whatever helps you work.
- The PR title becomes the commit subject. Look at `git log` for the style: sentence case,
  imperative, no trailing period ("Add resolve_board: token probes plus careers-page board
  links").
- The PR description becomes the commit body. It says what changed and why, and how you tested
  it. Say whether you ran the live tests, and paste the relevant output if you did.
- If a PR changes what a tool returns or accepts, it updates `docs/tools.md` too.
