# jobboard-mcp

A small, read-only [MCP](https://modelcontextprotocol.io) server that sweeps public applicant-tracking-system
job boards (Greenhouse, Ashby, Lever) for a watchlist of companies and returns clean, normalized, filtered jobs.

It talks only to the public JSON board APIs. The one exception is `resolve_board`, which reads a company's
careers page (and at most two same-site careers links) only to find which board it links to. It never scrapes
LinkedIn, Indeed, or job content from web pages, and the only file it writes is its own watchlist. See [SPEC.md](SPEC.md) for the design and
[docs/api-notes.md](docs/api-notes.md) for how the live APIs differ from their docs.

## Tools

| Tool | What it does |
|------|--------------|
| `sweep_boards` | Fetches every watched board in parallel, applies title / remote / pay / date / seen-before filters, and returns matching jobs newest first, with per-company status (`ok`, `not_found`, `no_board`, `error`, `timeout`). |
| `get_posting` | One posting by URL or `{ats, token, job_id}`, with its full plain-text description and the best pay data (Greenhouse pay-transparency ranges included). |
| `find_company_posting` | Finds a role seen on LinkedIn, Indeed, etc. on the company's own board (watchlist board, or resolved on the fly). Up to 5 fuzzy-matched candidates, scored 0-100. |
| `resolve_board` | Works out which Greenhouse, Ashby, or Lever board a company uses: probes likely tokens and scans the careers page (plus up to two same-site careers links) for board links. Read-only. |
| `is_posting_live` | Whether a posting is still open, via the ATS API. Ashby has no per-job endpoint, so it checks the (usually cached) board listing. |
| `list_watchlist` | Watched companies, filterable by board / tag / domain. |
| `add_company`, `update_company` | The only writes, and only to `data/watchlist.yaml`. Boards are validated with a live fetch before saving; comments and formatting are preserved. Not registered when `WATCHLIST_READONLY=true`. |

### Normalization rules

- **Remote:** `remote_us` needs positive evidence that a US-based worker is eligible: structured
  workplace type plus a US location or country, or text like "Remote, US", "North America", "AMER".
  A bare "Remote" is `unknown`. Unknown jobs are kept and flagged `remote_unknown`
  (pass `include_unknown_remote: false` to drop them).
- **Pay:** structured fields first (Ashby salary tiers, Lever `salaryRange`, Greenhouse
  `pay_input_ranges`), then ranges parsed from the description. `pay_min` and `pay_max` are annual
  USD only (hourly × 2080). Non-USD pay keeps its currency and raw text but no numbers. Multi-tier
  pay reports the overall range and keeps the tiers in `pay_raw`.
- **Titles:** plain filter terms match whole words plus a plural, so the default `intern` exclude
  keeps "Internal Tools Engineer". Prefix a term with `re:` for a regex.
- **Missing data** never silently drops a job. With `min_pay` or `posted_within_days` set, jobs
  without pay or a date are kept and flagged `pay_unknown` or `date_unknown`.

## Setup

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 for you.

```sh
uv sync
uv run pytest            # offline, against recorded fixtures
uv run pytest -m live    # opt-in: hits the real board APIs
uv run ruff check . && uv run pyright
```

The watchlist lives in [data/watchlist.yaml](data/watchlist.yaml).

## Use from the Claude desktop app

Add this to `claude_desktop_config.json` and restart the app. On Windows, the Microsoft Store build keeps
it at `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`.
Other installs use `%APPDATA%\Claude\claude_desktop_config.json`.

```json
{
  "mcpServers": {
    "jobboard": {
      "command": "C:\\Users\\Hakuj\\.local\\bin\\uv.exe",
      "args": ["--directory", "A:\\code\\jobboard-mcp", "run", "jobboard-mcp"]
    }
  }
}
```

If the file already has an `mcpServers` object, add the `"jobboard"` entry inside it.

## Configuration

Environment variables (all optional):

| Variable | Default | |
|----------|---------|---|
| `WATCHLIST_PATH` | `data/watchlist.yaml` | |
| `CACHE_TTL_SECONDS` | `10800` (3 h) | Boards are refetched at most once per TTL unless `refresh: true`. Stale entries revalidate with ETag / Last-Modified. |
| `MAX_CONCURRENCY` | `8` | Global cap on in-flight requests |
| `PER_HOST_CONCURRENCY` | `4` | Per-host cap |
| `HTTP_TIMEOUT_SECONDS` | `15` | Retries 429/5xx twice with backoff, honoring `Retry-After` |
| `LOG_LEVEL` | `INFO` | JSON logs go to stderr (stdout is the MCP transport) |
| `WATCHLIST_READONLY` | `false` | `true` removes `add_company` and `update_company` (for a public deployment) |

## Layout

```
src/jobboard_mcp/
  server.py      MCP tools and transport
  service.py     sweep orchestration (two phases: cheap filters, then Greenhouse pay details for survivors)
  adapters/      one module per ATS
  normalize.py   remote / pay / date / HTML helpers (pure)
  filters.py     sweep filters, flags, sorting (pure)
  http.py        shared client: cache, retries, concurrency caps
  watchlist.py   YAML loading, validation, and comment-preserving edits
  resolve.py     board detection (token probes + careers-page links)
  matching.py    fuzzy title matching (pure)
tests/fixtures/  trimmed real API responses
```
