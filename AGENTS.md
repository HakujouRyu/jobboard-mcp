# AGENTS.md

Instructions for AI coding agents working in this repo. People should start with
[CONTRIBUTING.md](CONTRIBUTING.md). It's the source of truth, and this file only adds what an
agent tends to get wrong.

jobboard-mcp is a read-only MCP server (Python 3.12, FastMCP, httpx, pydantic) that sweeps public
Greenhouse, Ashby, and Lever job board APIs, plus Workday careers sites' own JSON, and returns
normalized, filtered jobs.

## Commands

```sh
uv sync                          # set up
uv run pytest                    # offline tests, ~5 s; run after every change
uv run ruff check --fix . && uv run ruff format .
uv run pyright
uv run pytest -m live            # real APIs; only when you touch an adapter, http.py, urlguard.py, or resolve.py
```

All of pytest, ruff, and pyright must pass before you call a change done. A live-test failure
can mean a real board changed. Say so instead of "fixing" the code to match.

## Read before you change

| Before touching | Read |
|-----------------|------|
| Anything structural: a new module, a new tool, request flow | [docs/architecture.md](docs/architecture.md) |
| An adapter, or anything that reads ATS JSON | [docs/api-notes.md](docs/api-notes.md). The live APIs differ from their docs, and the notes record how. |
| `normalize.py` or `filters.py` | [docs/normalization.md](docs/normalization.md). It's the contract for those rules. |
| A tool's arguments, output, or description | [docs/tools.md](docs/tools.md) |
| `watchlist.py` or the watchlist tools | [docs/watchlist.md](docs/watchlist.md) |

## Ground rules

The full list is under "Ground rules" in CONTRIBUTING.md. The ones agents break most:

- **Never print to stdout.** It's the MCP transport. Log through `logging`.
- **Never guess `remote_us`.** Ambiguous means `unknown`. Missing data is flagged, never dropped.
- **Structured data only, best tier first.** Ground rule 1 has three tiers: documented public
  APIs, then the JSON a careers site loads for itself, then schema.org JSON-LD. Never use a
  lower tier for a system a higher one covers, and never read job content from page layout.
  Every request goes through `HttpClient`: careers pages only through `get_text`, and a
  careers site's JSON only through `get_site_json` / `post_site_json`, which keep them on
  public addresses, check robots.txt, and pace them. Don't add another way to fetch a URL.
- **Keep `normalize.py`, `filters.py`, `matching.py`, `robots.py`, and `models.py` pure.** No
  I/O, no clock.
- **Don't edit the user's watchlist** (`%APPDATA%\jobboard-mcp\watchlist.yaml` or
  `~/.config/jobboard-mcp/watchlist.yaml`, or an old `data/watchlist.yaml`). Change
  `src/jobboard_mcp/watchlist.example.yaml` only when asked, and keep one board per ATS in it,
  because the live tests use it.
- **Don't hand-edit `src/jobboard_mcp/boards_greenhouse.tsv`.** It's generated third-party
  data; rebuild it with `scripts/build_board_directory.py`.
- **Don't edit field values in `tests/fixtures/`.** They're real, trimmed API responses. To
  cover a new case, record a new fixture (see "Recording a fixture" in CONTRIBUTING.md).
- **Don't add dependencies** without saying why. Use `uv add`, never pip.

## Keep the docs and diagrams in sync

The docs are part of the change. Update them in the same commit as the code, including the
Mermaid diagrams, which go stale most easily:

| If you change | Update |
|---------------|--------|
| Modules or how they depend on each other | architecture.md: "Components" and "Module layers" diagrams, and the module table |
| The sweep sequence or caching | architecture.md: "A sweep, step by step" and "The HTTP client" diagrams |
| Careers-page fetching or the URL guard | architecture.md: "The HTTP client" diagram and "Careers pages"; SECURITY.md |
| `resolve_board` logic | architecture.md: "Board resolution" diagram; tools.md |
| Models or exceptions | architecture.md: "Data model" and "Errors" diagrams |
| Remote, pay, date, or filter rules | normalization.md and its diagrams |
| Watchlist editing | watchlist.md: "How edits work" diagram |
| A tool's interface | tools.md, the README's tool table, and `INSTRUCTIONS` in server.py |
| A setting | `config.py` and the README's configuration table |
| Anything a user would notice | CHANGELOG.md, under Unreleased |

## Commits

Small commits that each do one thing. Subjects are sentence case and imperative, with no
trailing period. PRs are squash-merged, so a PR's title and description become the commit on
`main` and follow the same style. See `git log` and "Commits and pull requests" in
CONTRIBUTING.md.
