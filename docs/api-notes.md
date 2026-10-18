# ATS API notes (verified 2026-10-15; Workday 2026-10-17)

Checked against live responses before writing the adapters. Fixtures in `tests/fixtures/` are
trimmed copies of these responses (whole job objects, fewer of them).

| Fixture | Source |
|---------|--------|
| `greenhouse_gitlab_list.json` | `boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true` (16 of 218 jobs, one per distinct location string, plus US engineering roles) |
| `greenhouse_gitlab_detail.json` | detail with `pay_transparency=true`, a US remote role |
| `greenhouse_gitlab_detail_nopay.json` | detail for a Bangalore role (`pay_input_ranges: []`) |
| `ashby_linear.json` | `api.ashbyhq.com/posting-api/job-board/linear?includeCompensation=true` (8 of 31; no compensation published) |
| `ashby_temporal.json` | same for `temporal` (14 of 69; multi-tier USD pay, GBP/EUR/INR/SGD pay, hybrid) |
| `lever_outreach.json` | `api.lever.co/v0/postings/outreach?mode=json` (8 of 24; has `salaryRange`) |
| `lever_leverdemo.json` | Lever's own demo board (10 jobs; `workplaceType: "unspecified"`, odd `commitment` values) |
| `lever_empty.json` | `mistral` board: 200 with `[]` |
| `workday_cox_search.json` | `POST cox.wd1.myworkdayjobs.com/wday/cxs/cox/Cox_External_Career_Site_1/jobs`, `searchText: "software engineer"` (4 of 12 rows; `facets` dropped) |
| `workday_cox_detail.json` | `GET .../job/Atlanta-GA/Senior-Software-Engineer_R202683513` (no `remoteType`; pay in the description; `similarJobs` dropped) |
| `workday_homedepot_detail.json` | `GET homedepot.wd5.myworkdayjobs.com/.../CareerDepot/job/...` (`remoteType: "Remote/Virtual"`; `similarJobs` dropped) |
| `workday_empty.json` | the Cox search for a word that matches nothing: 200, `total: 0` |
| `workday_422.json` | a tenant that doesn't exist |
| `*_404.*` | unknown-token responses (for Workday, an unknown site) |

## Differences from SPEC.md §3

### Greenhouse
- `first_published` is present on the **list** endpoint for every job, so `posted_at` needs no detail call.
- Pay on the detail endpoint is `pay_input_ranges: [{min_cents, max_cents, currency_type, title, blurb}]`.
  Values are in **cents**. There is no interval field. `title` names the region ("United States Salary Range").
- GitLab renders the same pay range into `content` ("United States Salary Range $X – $Y USD"), so text
  parsing finds pay on the list endpoint for most US roles.
- `location.name` packs multiple locations with `;` ("Remote, Canada; Remote, United States").
- No structured remote or employment-type field. Remote comes from location text only.
- Unknown token → 404 `{"status":404,"error":"Job not found"}`. Unknown job id → 404.

### Ashby
- **`isRemote` is unreliable**: Temporal marks hybrid San Francisco roles `isRemote: true`.
  `workplaceType` (`Remote` / `Hybrid` / `OnSite`) is authoritative; `isRemote` is a fallback only.
- Location is often a region ("North America", "Europe") or "United States (Remote)".
  `address.postalAddress.addressCountry` is structured and present.
- `secondaryLocations: [{location, address}]` carries extra locations.
- Compensation: `compensation.compensationTiers[].components[]` with `compensationType`
  (`Salary`, `Bonus`, `Commission`, `EquityCashValue`), `interval` ("1 YEAR"), `currencyCode`,
  `minValue`, `maxValue`. Only `Salary` components count toward `pay_min`/`pay_max`.
  `summaryComponents` already holds the overall min/max across tiers.
- `employmentType`: `FullTime`, `PartTime`, `Intern`, `Contract`, `Temporary`.
- `isListed` exists; unlisted jobs are skipped.
- `workplaceType` and `address` can be null (Supabase, Notion). `addressCountry` can be a list
  in one string (PostHog: `"US | EU"`). Regions use shorthands like `AMER`, `APAC`, `EMEA`.
- Unknown token → 404 with plain-text body `Not Found`.
- On Windows, curl (schannel) can fail TLS revocation checks against this host. httpx is unaffected.

### Lever
- Many large Lever boards (Spotify, Palantir, Veeva) publish no `salaryRange` at all.
- `salaryRange: {min, max, currency, interval}` with `interval` like `per-year-salary`.
  `salaryDescriptionPlain` sometimes carries prose about pay.
- `workplaceType`: `remote`, `hybrid`, `onsite`, and also **`unspecified`**.
- `country` is an ISO-3166 alpha-2 code for the primary location; `categories.allLocations` lists all.
- `categories.commitment` is free text ("Full-Time", "Permanent", "Regular Full Time (Salary)", even "Remote").
- `createdAt` is epoch milliseconds (creation, which may precede publishing).
- Empty board → 200 `[]`. Unknown token → 404 `{"ok":false,"error":"Document not found"}`.

### Workday (verified 2026-10-17; tier 2, undocumented)

Workday has no documented public jobs API. Each careers site loads its jobs from JSON under
`https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}`, with no sign-in. These
endpoints are undocumented and can change without notice; the weekly live tests are what would
catch it. Checked against Ecolab, Home Depot, Equifax, Fiserv, Cox, and QBE.

- **robots.txt** disallows the human-facing pages (`/{site}/`, `/refreshFacet/`) and leaves
  `/wday/cxs/` open. It answers `Accept: text/plain` alone with **406**, which would read as "no
  rules", so robots.txt is fetched with `text/plain,*/*;q=0.5`.
- **Search:** `POST .../jobs` with `{"appliedFacets": {}, "limit": 20, "offset": N,
  "searchText": "..."}`. `limit` above 20 → **400**. The response is `{total, jobPostings,
  facets, userAuthenticated}`. `searchText` matches more than titles, and results come in
  relevance order. Totals can be inconsistent (Home Depot reported 980 with an empty search but
  still served a page at offset 2000), so the adapter stops at a short page or at 200 rows.
- **List rows** have only `title`, `externalPath`, `timeType`, `locationsText` (one location,
  `"20 Locations"`, or null), `postedOn` (`"Posted Today"`, `"Posted Yesterday"`,
  `"Posted N Days Ago"`, `"Posted 30+ Days Ago"`), and `bulletFields` (the requisition id).
- **Detail:** `GET .../job/{jobPostingId}` (the location segment of `externalPath` can be left
  out). `jobPostingInfo` has `title`, `jobDescription` (HTML; pay, when posted, is in the text),
  `location`, `additionalLocations`, `startDate` (the posting date, `YYYY-MM-DD`), `timeType`,
  `country.descriptor`, `externalUrl`, `posted`, `canApply`, and on some tenants `remoteType`.
- **`remoteType`** is tenant-defined text: Home Depot uses "Onsite", "Remote/Virtual", "Hybrid",
  "Multi-site", and "Location Independent-Commutable". Equifax and Cox don't use it.
  `jobRequisitionLocation` can say more ("USA-Remote Worker-Illinois" behind a Chicago location
  at Ecolab), but it's the requisition's home, not where the job is advertised, so it isn't read.
- **Tokens:** the site name is case-insensitive; the tenant in the path must be lowercase
  (uppercase → 502). `wdN.myworkdaysite.com/wday/cxs/{tenant}/{site}` serves the same data.
- **Errors:** unknown site → 404 `{"errorCode":"S21", "message":"not found:
  Job_Posting_Site_ID=..."}`; unknown tenant → **422**; unknown job → 404.
- Some company careers pages (Ecolab's) answer 403 to non-browser clients, so `resolve_board`
  can't always find a Workday board from the page. A job link works through `get_posting`.
