# Tool reference

Every tool returns structured JSON (the Pydantic models in
[models.py](../src/jobboard_mcp/models.py)). Expected failures, such as a bad URL, an unknown
company, or a board that fails validation, come back as MCP tool errors with a readable message.

| Tool | Reads | Writes |
|------|-------|--------|
| [`sweep_boards`](#sweep_boards) | watchlist, every board | — |
| [`get_posting`](#get_posting) | one posting | — |
| [`find_company_posting`](#find_company_posting) | watchlist, one board, maybe a careers page | — |
| [`resolve_board`](#resolve_board) | board endpoints, a careers page | — |
| [`is_posting_live`](#is_posting_live) | one posting or board | — |
| [`list_watchlist`](#list_watchlist) | watchlist | — |
| [`add_company`](#add_company) | the new board | `data/watchlist.yaml` |
| [`update_company`](#update_company) | the new board, if changed | `data/watchlist.yaml` |

Read tools are annotated `readOnlyHint: true, idempotentHint: true`. The two write tools are
`readOnlyHint: false, destructiveHint: false`, and aren't registered at all when
`WATCHLIST_READONLY=true`.

Every tool that fetches takes `refresh: true` to skip the 3-hour cache.

---

## `sweep_boards`

Fetches every watched board (or the ones named) in parallel and returns matching jobs, newest
first, with a status for each company. One company failing never fails the sweep.

| Parameter | Type | Default | |
|-----------|------|---------|---|
| `companies` | string[] | every company that isn't archived | Watchlist names, case-insensitive. Archived companies can be named. A name that isn't on the watchlist gets status `error`. |
| `title_include` | string[] | none | Keep titles matching **any** term. See [title terms](normalization.md#title-terms). |
| `title_exclude` | string[] | `intern`, `internship`, `new grad`, `principal` | Drop titles matching any term. `[]` excludes nothing. |
| `remote` | enum[] | `remote_us`, `remote_global` | Remote statuses to keep. |
| `include_unknown_remote` | bool | `true` | Keep `unknown` remote jobs (flagged `remote_unknown`). |
| `min_pay` | int ≥ 0 | none | Annual USD. Drops jobs whose posted **maximum** is below it. Unknown and non-USD pay are kept (flagged `pay_unknown` / `pay_not_usd`). |
| `posted_within_days` | int ≥ 0 | none | Unknown dates are kept and flagged. |
| `exclude_ids` | string[] | none | `{ats}:{token}:{id}` values already seen. |
| `exclude_fingerprints` | string[] | none | Fingerprints already seen. |
| `exclude_urls` | string[] | none | Posting or apply URLs already seen. Matched without scheme, query, or trailing slash. |
| `include_description` | bool | `false` | Include `description_text`. Makes the output much larger. |
| `refresh` | bool | `false` | |

Example call:

```json
{
  "title_include": ["engineer", "sre", "product"],
  "remote": ["remote_us", "remote_global"],
  "min_pay": 100000,
  "posted_within_days": 21
}
```

Example result, abridged. The job is real (from the recorded GitLab fixture). The
`per_company` rows show one of each status, and their counts are illustrative.

```json
{
  "jobs": [
    {
      "id": "greenhouse:gitlab:8710061002",
      "fingerprint": "e829bd5219f30812",
      "company": "GitLab",
      "ats": "greenhouse",
      "board_token": "gitlab",
      "native_id": "8710061002",
      "title": "Migration Engineer - GitLab Dedicated",
      "team": "Dedicated Migrations",
      "locations": ["Remote, United States"],
      "remote": "remote_us",
      "employment_type": null,
      "posted_at": "2026-08-19",
      "updated_at": "2026-10-15",
      "date_source": "first_published",
      "pay_min": 126400,
      "pay_max": 213600,
      "pay_currency": "USD",
      "pay_posted_interval": "year",
      "pay_raw": "$126,400–$213,600 USD",
      "pay_source": "parsed_from_text",
      "url": "https://job-boards.greenhouse.io/gitlab/jobs/8710061002",
      "apply_url": null,
      "description_text": null,
      "flags": []
    }
  ],
  "per_company": [
    { "company": "GitLab", "ats": "greenhouse", "total": 218, "matched": 6, "status": "ok", "error": null },
    { "company": "Gone Co", "ats": "greenhouse", "total": 0, "matched": 0, "status": "not_found", "error": "HTTP 404" },
    { "company": "Careers Page Co", "ats": null, "total": 0, "matched": 0, "status": "no_board", "error": null }
  ],
  "fetched_at": "2026-10-15T18:00:00Z",
  "cache_hits": 0,
  "flag_counts": { "pay_unknown": 3, "remote_unknown": 2 }
}
```

`per_company[].status` is one of:

| Status | Meaning |
|--------|---------|
| `ok` | Board fetched. `total` is every listed job, `matched` is how many were returned. |
| `not_found` | The board token returned 404. The company may have moved to another board: try `resolve_board`. |
| `no_board` | The watchlist entry has no `ats`/`token`. Nothing was fetched. |
| `timeout` | No response within `HTTP_TIMEOUT_SECONDS`. |
| `error` | Anything else (5xx after retries, invalid JSON, a name not on the watchlist). See `error`. |

---

## `get_posting`

One posting with its full plain-text description and the best pay data available. For
Greenhouse, that means the detail endpoint with `pay_transparency=true`.

Pass **either** `url` **or** all of `ats`, `token`, and `job_id`.

| Parameter | Type | |
|-----------|------|---|
| `url` | string | A posting URL (see below) |
| `ats` | `greenhouse` \| `ashby` \| `lever` | |
| `token` | string | Board token, such as `gitlab` |
| `job_id` | string | The ATS's own id |
| `refresh` | bool | |

Recognized URLs:

| ATS | Forms |
|-----|-------|
| Greenhouse | `boards.greenhouse.io/{token}/jobs/{id}`, `job-boards.greenhouse.io/...`, `job-boards.eu.greenhouse.io/...`, `.../embed/job_app?for={token}&token={id}`, `boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}` |
| Ashby | `jobs.ashbyhq.com/{token}/{uuid}` |
| Lever | `jobs.lever.co/{token}/{uuid}` |

Returns a `Job` (the same shape as above, with `description_text` filled in). `company` is the
watchlist display name when the board is on the watchlist, and the token otherwise.

Ashby has no public per-job endpoint, so the job is looked up in the board listing.

---

## `find_company_posting`

Finds a role you saw on LinkedIn, Indeed, or elsewhere on the company's own board. It uses the
watchlist board if there is one, and calls `resolve_board` if not.

| Parameter | Type | |
|-----------|------|---|
| `company` | string | As shown on the other site |
| `title` | string | As shown on the other site |
| `location` | string? | Optional. Breaks ties (15% of the score). |
| `refresh` | bool | |

Titles are normalized before scoring: "Sr." becomes "senior", SWE/SRE/PM/EM are expanded, roman
numeral levels become digits, and noise such as "(Remote)", "US", or "Full Time" is dropped. The
score is 60% rapidfuzz `token_sort_ratio` plus 40% `token_set_ratio`, minus 15 points when the
seniority words differ ("Senior" vs "Staff") and 15 when the role words differ ("Product
Manager" vs "Product Marketing Manager"). Up to 5 candidates scoring 55 or more are returned.

How a query of `Sr. Product Engineer (Remote)` scores against some board titles:

| Board title | `title_score` | |
|-------------|---------------|---|
| Senior Product Engineer | 100.0 | "Sr." expanded, "(Remote)" dropped |
| Senior Product Designer | 72.0 | role word differs (−15) |
| Staff Product Engineer | 66.7 | seniority differs (−15) |
| Product Manager | 48.3 | below 55, not returned |

Result shape (abridged):

```json
{
  "company": "Linear",
  "title": "Sr. Product Engineer (Remote)",
  "board_found": true,
  "ats": "ashby",
  "token": "linear",
  "resolution": null,
  "candidates": [
    { "score": 100.0, "title_score": 100.0, "location_score": null, "job": { "title": "Senior Product Engineer", "...": "..." } },
    { "score": 66.7, "title_score": 66.7, "location_score": null, "job": { "title": "Staff Product Engineer", "...": "..." } }
  ]
}
```

`resolution` is filled in when the board wasn't on the watchlist and had to be resolved.
`board_found: false` means no board could be found, or the board returned 404.

---

## `resolve_board`

Works out which Greenhouse, Ashby, or Lever board a company uses. It never writes; use
`update_company` to save the result. The method is described in
[architecture.md § Board resolution](architecture.md#board-resolution).

| Parameter | Type | |
|-----------|------|---|
| `company` | string? | |
| `careers_url` | string? | Defaults to the watchlist's `careers_url` for the company |
| `refresh` | bool | |

At least one of `company` or `careers_url` is required.

Example result:

Example: a live run of `{"company": "Posit", "careers_url": "https://posit.co/careers/"}` on
2026-10-16. The first page had no board link, so the tool followed a same-site careers link and
found Posit's board, which still uses its old name, RStudio. Guessing the token from the name
alone would have missed it.

```json
{
  "ats": "greenhouse",
  "token": "rstudio",
  "confidence": "high",
  "jobs": 8,
  "evidence": [
    "careers page https://posit.co/about/careers links to greenhouse board 'rstudio'",
    "board answered (8 jobs)",
    "board reports company name 'Posit'"
  ],
  "alternatives": [],
  "pages_checked": ["https://posit.co/careers/", "https://posit.co/about/careers"]
}
```

| Confidence | Meaning |
|------------|---------|
| `high` | The careers page links to the board, or a Greenhouse board reports the same company name |
| `medium` | A guessed token answered with a non-empty board |
| `low` | A guessed token exists but the board is empty |

When nothing is found, `ats` and `token` are null, and `evidence` lists the tokens tried and any
unsupported ATS the pages mention (Workday, Workable, and so on).

Careers pages are only fetched from public addresses over `http`/`https` on the default port,
redirects included (see [architecture.md](architecture.md#careers-pages)). A page that's refused
still counts as checked, with the reason, and the token probes still run:

```json
"pages_checked": ["http://localhost:8080/careers (blocked: only default ports are fetched, not 8080)"]
```

---

## `is_posting_live`

Whether a posting is still open, checked through the ATS API rather than the posting page. It
takes the same arguments as `get_posting`.

```json
{
  "live": false,
  "checked_at": "2026-10-16T14:02:11Z",
  "reason": "greenhouse returned HTTP 404 for the job",
  "ats": "greenhouse",
  "token": "gitlab",
  "job_id": "8710061002",
  "from_cache": false
}
```

Greenhouse and Lever check the job's own endpoint. Ashby has no per-job endpoint, so it checks
whether the job is still in the board listing, which is usually cached. Pass `refresh: true` for
a fresh answer. `from_cache` says whether the answer came from the cache.

---

## `list_watchlist`

| Parameter | Type | |
|-----------|------|---|
| `has_board` | bool? | `true`: only companies with a known board. `false`: only careers-page entries. |
| `tag` | string? | Exact tag, case-insensitive, such as `hww` |
| `domain` | string? | Substring of any domain, such as `infra` |
| `include_archived` | bool | Also list archived companies. Default `false`. |

Returns `{ companies: WatchlistEntry[], total, readonly, archived_hidden }`. `archived_hidden`
counts the archived companies left out.

---

## `add_company`

Adds a company to the end of the watchlist. If `ats` and `token` are given, the board is fetched
live first, and nothing is saved unless that fetch succeeds.

| Parameter | Type | |
|-----------|------|---|
| `company` | string | Display name. Must not already be on the watchlist. |
| `ats`, `token` | | Both or neither |
| `careers_url` | string? | |
| `domains`, `tags` | string[]? | |
| `interview_note`, `notes` | string? | |

Returns `{ entry, board_check: { status, jobs, error } | null }`.

## `update_company`

Changes only the fields you pass. A new or changed board is validated live before anything is
saved.

| Parameter | |
|-----------|---|
| `company` | Current name on the watchlist |
| `new_name` | Rename |
| `ats`, `token` | Set the board |
| `clear_board` | `true` removes `ats` and `token`. Can't be combined with them. |
| `careers_url`, `interview_note`, `notes` | `""` clears |
| `domains`, `tags` | Replace the list. `[]` clears. |
| `archived` | `true` archives: sweeps skip the company and `list_watchlist` hides it. `false` restores it. This is how to drop a company; there's no delete. |

Both write tools keep the file's comments and layout and replace it atomically. See
[watchlist.md](watchlist.md).
