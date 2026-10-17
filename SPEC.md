# jobboard-mcp — Design Spec

**Status:** Draft v1 · **Date:** 2026-10-15

This is the original design. Where the code has since moved on, [docs/](docs/) describes what it
actually does.

## 1. Purpose

A small, read-only MCP server that searches public applicant-tracking-system (ATS) job boards for a set of watched companies and returns clean, normalized, filtered job data.

It replaces the slowest and least reliable part of a job search run through an AI assistant, which otherwise fetches each company's board one at a time with a generic web fetch. Large boards come back truncated, and parsing dates, pay, and remote status is redone (inconsistently) every run.

## 2. Goals and non-goals

**Goals**
- One tool call sweeps every watched company and returns only matching jobs.
- Deterministic: the same boards and filters always produce the same output.
- Pay, posted date, and remote status parsed in code, with the raw values kept.
- Works locally (stdio, via the Claude desktop app) and hosted (streamable HTTP, as a claude.ai custom connector).
- Tested offline against recorded fixtures.

**Non-goals**
- No scraping of LinkedIn, Indeed, Glassdoor, or any site whose terms forbid it, and no HTML scraping of careers pages beyond detecting which ATS they link to.
- No writes anywhere except the server's own watchlist file. The server touches public data only.
- No accounts, credentials, résumés, or applying to jobs.
- No judging fit or legitimacy. That stays with the MCP client and its user.

## 3. Supported job boards (v1)

All are public and need no auth. **Verify field names against live responses before relying on them**; the notes below are a starting point, not a contract.

| ATS | List endpoint | Detail endpoint | Notes |
|-----|---------------|-----------------|-------|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | `/v1/boards/{token}/jobs/{id}?pay_transparency=true` | `content` is HTML-escaped HTML. Dates: `updated_at`, possibly `first_published`. Pay only on detail with `pay_transparency`. |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true` | Same payload (filter by id) | Has `isRemote`, `publishedAt`, `jobUrl`, `applyUrl`, `descriptionPlain`, compensation tiers. |
| Lever | `https://api.lever.co/v0/postings/{token}?mode=json` | `/v0/postings/{token}/{id}` | `createdAt` in epoch ms, `workplaceType`, `categories.location/commitment/team`, optional `salaryRange`. |

Stretch (v2): Workable, SmartRecruiters, Recruitee, and Workday (Workday's endpoints are per-tenant and less stable, so it may stay out of scope).

## 4. Data model

### 4.1 `Job` (normalized output)

| Field | Type | Notes |
|-------|------|-------|
| `id` | str | `{ats}:{token}:{native_id}` |
| `fingerprint` | str | Hash of normalized company + title + location, for cross-source dedupe |
| `company` | str | Display name from the watchlist |
| `ats` | enum | `greenhouse`, `ashby`, `lever` |
| `board_token` | str | |
| `native_id` | str | |
| `title` | str | |
| `team` | str or null | Department or team when given |
| `locations` | list[str] | Raw location strings |
| `remote` | enum | `remote_us`, `remote_global`, `remote_other`, `hybrid`, `onsite`, `unknown` |
| `employment_type` | enum or null | `full_time`, `part_time`, `contract`, `intern`, `unknown`. A title marked "(Contract)" means `contract` |
| `posted_at` | date or null | Best available first-published date |
| `updated_at` | date or null | |
| `date_source` | str | Which raw field `posted_at` came from |
| `pay_min`, `pay_max` | int or null | Annual, in `pay_currency`. Convert hourly at 2080 hours. Never convert currencies. |
| `pay_currency`, `pay_posted_interval` | str or null | As posted (`pay_min`/`pay_max` are always annualized). USD when the posting offers it, else the first currency posted. |
| `pay_other_currencies` | list | `{currency, min, max, interval}` for each other currency the posting lists, annualized the same way |
| `pay_raw` | str or null | Original text or structure, for auditing |
| `pay_source` | enum | `structured`, `parsed_from_text`, `none` |
| `url` | str | Human-readable posting URL |
| `apply_url` | str or null | |
| `description_text` | str or null | Plain text. Detail calls only, unless requested. |

### 4.2 Watchlist entry

Stored in `data/watchlist.yaml`, the server's source of truth. The repo ships `data/watchlist.example.yaml` as a starting point.

```yaml
- company: Temporal Technologies
  ats: ashby          # greenhouse | ashby | lever | null
  token: temporal     # null if unknown
  careers_url: https://temporal.io/careers
  domains: [dev tools, open source]
  tags: []            # e.g. [hww] for hiring-without-whiteboards
  interview_note: null
  notes: Old Greenhouse board is gone
```

## 5. Tools

All tools return JSON and are read-only except the watchlist tools in §5.6. Errors for one company never fail a whole sweep (see §6).

### 5.1 `sweep_boards`
Fetch every watched board (or a named subset) in parallel and return matching jobs.

Input:
- `companies`: list[str], optional (default: entire watchlist)
- `title_include`: list[str], optional. Case-insensitive whole-word terms (never regexes), any of which must match.
- `title_exclude`: list[str], optional. No default.
- `remote`: list of enum, optional (default: every status)
- `min_pay`: int, optional. Drops jobs whose posted **max** is below this; jobs with no pay are kept and flagged.
- `posted_within_days`: int, optional. Jobs with an unknown date are kept and flagged.
- `exclude_ids`, `exclude_fingerprints`, `exclude_urls`: lists, optional (leads the client has already seen)
- `include_description`: bool, default false

Output: `{ jobs: Job[], per_company: [{company, ats, total, matched, status, error?}], fetched_at, cache_hits }`

Sort jobs by `posted_at` descending, unknown last.

### 5.2 `get_posting`
Input: `ats`, `token`, `job_id` (or a posting `url` that it parses). Output: one full `Job` including `description_text` and the best pay data (Greenhouse detail with pay transparency).

### 5.3 `find_company_posting`
Find a role (seen on LinkedIn, Indeed, etc.) on the company's own board.

Input: `company`, `title`, optional `location`. Resolve the board (watchlist first, then `resolve_board`), then fuzzy-match titles (normalize seniority words, punctuation, and "Sr." vs "Senior").

Output: up to 5 candidates with match scores, plus `board_found`: bool.

### 5.4 `resolve_board`
Input: `company` and/or `careers_url`.

Method, in order:
1. Probe likely tokens (slugified name and simple variants) against each ATS list endpoint. A non-empty valid response wins.
2. If a `careers_url` is given, fetch that one page and look for links or embeds pointing at `boards.greenhouse.io`, `job-boards.greenhouse.io`, `jobs.ashbyhq.com`, or `jobs.lever.co`.

Output: `{ ats, token, confidence, evidence }` or `{ ats: null }`. Read-only: it does **not** modify the watchlist.

### 5.5 `is_posting_live`
Input: `url` or `{ats, token, job_id}`. Output: `{ live: bool, checked_at, reason }`. Uses the ATS API, not a page fetch, where possible.

### 5.6 `list_watchlist` / `add_company` / `update_company`
Manage `data/watchlist.yaml`. `add_company` validates the board with a live fetch before saving. These are the only tools that write, and they write only the server's own watchlist file.

When hosted, these can be disabled with a config flag (`WATCHLIST_READONLY=true`) so the public endpoint can't be edited.

### 5.7 `lookup_business_address` (optional, v1.1)
For forms that ask for an employer's address when the role is remote, such as unemployment work-search logs.

Input: `company`. Look up a CIK via `https://www.sec.gov/files/company_tickers.json`, then read `https://data.sec.gov/submissions/CIK{10-digit}.json` → `addresses.business`.

Output: `{ street1, street2, city, state, zip, source_url }` or `{ found: false }`. Public companies only. SEC requires a descriptive `User-Agent` with contact info; read it from config and never hard-code it.

## 6. Behavior

- **Concurrency:** async HTTP (httpx) with a global concurrency cap (default 8) and a per-host cap (default 4).
- **Timeouts and retries:** 15s timeout; retry with backoff on 429 and 5xx (max 2 retries), honoring `Retry-After`.
- **Caching:** in-memory or SQLite cache keyed by URL, default TTL 3 hours, respecting `ETag` and `Last-Modified` where offered. A `refresh: true` argument bypasses it.
- **Partial failure:** each company reports `status` (`ok`, `not_found`, `error`, `timeout`) in `per_company`; the sweep still returns everything else.
- **Normalization rules:**
  - Remote: structured fields first (`isRemote`, `workplaceType`), then location strings ("Remote", "Remote - US", "United States", "Anywhere"). Ambiguous → `unknown`; never guess `remote_us`.
  - Pay: structured first; otherwise parse ranges like "$150,000 - $180,000", "$150K–$180K", or "$75/hr" from text. Multiple tiers (e.g. by US region): report the overall min and max and keep the tiers in `pay_raw`.
  - HTML to text: unescape, strip tags, and collapse whitespace.
- **Politeness:** identify with a `User-Agent` like `jobboard-mcp/{version} (+repo URL)`. No more than one full sweep per board per cache TTL unless `refresh` is set.
- **Logging:** structured JSON logs (company, ATS, status, latency, cache hit). Optional OpenTelemetry traces are a nice-to-have.

## 7. Architecture

```
src/jobboard_mcp/
  server.py        # FastMCP app, tool definitions, transport selection
  models.py        # Pydantic models: Job, WatchlistEntry, results
  adapters/
    base.py        # Adapter protocol: list_jobs(token), get_job(token, id)
    greenhouse.py
    ashby.py
    lever.py
  normalize.py     # remote / pay / date / html helpers (pure functions)
  filters.py       # sweep filtering + fingerprinting (pure functions)
  resolve.py       # board detection
  watchlist.py     # YAML load/save/validate
  sec.py           # optional address lookup
  http.py          # shared client, cache, retries, rate limits
data/watchlist.example.yaml
tests/
  fixtures/        # recorded JSON responses per ATS (real, trimmed)
  test_adapters.py
  test_normalize.py
  test_filters.py
  test_tools.py    # end-to-end tool calls against fixtures (no network)
```

- **Language and stack:** Python 3.12+, the official MCP Python SDK (`mcp`, FastMCP), httpx, pydantic v2, PyYAML. Managed with `uv`, tested with pytest + respx (HTTP mocking), linted with ruff, type-checked with mypy or pyright.
- **Transports:** `stdio` by default, for the Claude desktop app; `streamable-http` when run with `--http` or `MCP_TRANSPORT=http`, for hosting.
- **Config:** environment variables, documented in the README (`WATCHLIST_PATH`, `CACHE_TTL_SECONDS`, `MAX_CONCURRENCY`, `SEC_USER_AGENT`, `WATCHLIST_READONLY`, `ALLOW_CUSTOM_URLS`, `LOG_LEVEL`).

## 8. Deployment

1. **Local first:** register it in the Claude desktop app's MCP config as a stdio server. This works only when the computer is on.
2. **Hosted later:** Dockerfile plus a Fly.io (or Cloudflare) deploy. Add it to claude.ai as a custom connector so scheduled cloud runs can use it. Check claude.ai's current custom-connector authentication requirements at that point. Since the server only serves public data, a read-only deployment without auth is low-risk; keep watchlist writes disabled on the public endpoint.

## 9. Testing and acceptance criteria

- Unit tests for every normalization rule, including messy real-world location and pay strings collected from fixtures.
- Adapter tests against recorded fixtures for each ATS, including an empty board and a 404 token.
- Sweep test: about 15 watched companies, all fixtures, completing in under 1s offline.
- **Live smoke test** (opt-in, `pytest -m live`): sweep the real watchlist and assert at least 80% of companies return `ok`.
- **Acceptance:** `sweep_boards` over the current watchlist with filters `title_include=["engineer","sre","product"]`, `remote=["remote_us","remote_global"]`, `min_pay=100000`, `posted_within_days=21` returns in under 20s cold and under 2s warm, with no truncated boards.
- An MCP Inspector session shows all tools with clear descriptions and input schemas.

## 10. Milestones

1. **M1:** Adapters for Greenhouse, Ashby, and Lever, plus `normalize`, `sweep_boards`, `get_posting`, with fixtures, tests, and stdio transport.
2. **M2:** `find_company_posting`, `resolve_board`, `is_posting_live`, and the watchlist tools.
3. **M3:** HTTP transport, Dockerfile, deploy, README with architecture diagram and example outputs.
4. **M4 (optional):** `lookup_business_address`, OpenTelemetry, v2 ATS adapters.

## 11. Integration notes (for later)

Once the server is working, an assistant's job-search workflow can replace its per-company board fetches with one `sweep_boards` call, and "find the company's own posting" with `find_company_posting`. Vetting, ranking, and tracking applications stay with the client.
