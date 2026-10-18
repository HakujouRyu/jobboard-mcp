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
| `workable_huggingface.json` | `www.workable.com/api/accounts/huggingface?details=true` (all 6 jobs; US and EMEA remote roles, hidden locations) |
| `workable_404.txt` | an account that doesn't exist |
| `rippling_rippling.json` | `api.rippling.com/platform/api/ats/v2/board/rippling/jobs?pageSize=1000&groupJobsByLocation=true` (12 of 364 jobs; mixed workplace types per job, US-state and city-level remote, non-US remote, hybrid) |
| `rippling_empty.json` | the `demo` board: 200 with `items: []` |
| `rippling_404.json` | a board that doesn't exist |
| `oracle_atlanta_search.json` | `GET ehxr.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions`, `findReqs;siteNumber=CX_1`, `keyword="engineer"` (4 of 25 rows; facets dropped) |
| `oracle_atlanta_detail.json` | `GET .../recruitingCEJobRequisitionDetails`, `ById;Id="34378"` (pay in the description) |
| `oracle_not_found.json` | the same for an id that isn't posted: 200 with `items: []` |
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

### Workable (verified 2026-10-17)

- Workable's help center documents `https://www.workable.com/api/accounts/{subdomain}?details=true`
  (also `/locations` and `/departments`). It redirects to
  `apply.workable.com/api/v1/widget/accounts/{subdomain}`, the feed Workable's own job widget
  uses, with no key. robots.txt on both hosts allows it.
- The answer is `{name, description, jobs}`. Each job has `title`, `shortcode` (the id),
  `employment_type`, `telecommuting` (a boolean), `department`, `url` (the short
  `apply.workable.com/j/{shortcode}` form), `application_url`, `published_on` and `created_at`
  (`YYYY-MM-DD`), `country`, `city`, `state`, `locations[]` (`country`, `countryCode`, `city`,
  `region`, `hidden`), and with `details=true`, `description` (HTML).
- No structured pay; it's parsed from the description when posted there.
- `hidden: true` on a location means the employer hid it on the posting. The adapter keeps only
  the country for those.
- The short job URL doesn't name the account, so job URLs are built in the long form,
  `apply.workable.com/{account}/j/{shortcode}/`, which Workable also serves.
- Unknown account: the redirect ends in **404** with body `Not Found`.

### Rippling (verified 2026-10-17)

- Rippling documents the v2 job board API in its developer docs
  (`developer.rippling.com/documentation/job-board-api-v2/reference/job-boards`; the OpenAPI
  file is `developer.rippling.com/docs/job-board-api-v2.yaml`):
  `GET https://api.rippling.com/platform/api/ats/v2/board/{board_slug}/jobs`, plus `/branding`,
  `/locations`, and `/departments`. No key. The docs say the endpoints need a Recruiting Pro
  subscription, which is the employer's plan, not the caller's. v1 is marked deprecated.
- robots.txt on `ats.rippling.com` disallows only `/internal/`; `api.rippling.com` has none
  (404).
- Query parameters: `page` (from 0), `pageSize` (1 to 1000; 20 if left out), `searchQuery`,
  `workplaceType`, `country`, `state`, `city`, `departments`, and `groupJobsByLocation`.
  Without `groupJobsByLocation=true` there's one row per job and location (Rippling's own
  board: 640 rows for 364 jobs). The adapter asks for 1000 grouped rows, so one request
  covers a board, and reads up to 5 pages if there are more.
- The docs say results come in no particular order, so pages can overlap; the adapter
  drops repeats. They also give a rate limit of 100 requests every 10 minutes.
- The answer is `{items, page, pageSize, totalItems, totalPages}`. Each item has `id` (a
  UUID), `name` (the title, often with stray spaces; missing from the OpenAPI schema but
  always there), `url` (`ats.rippling.com/{slug}/jobs/{id}`), `department.name`, `language`
  (`en-US`, `en-GB`), and `locations[]` with `name`, `country`, `countryCode`, `state`,
  `stateCode`, `city`, and `workplaceType` (`ON_SITE`, `HYBRID`, or `REMOTE`).
- The list has no description, pay, posting date, or employment type, and the docs have no
  per-job endpoint. `.../v2/board/{slug}/jobs/{id}` does answer, with the description,
  `payRangeDetails`, `createdOn`, and `employmentType`, but it's undocumented, and job pages
  on `ats.rippling.com` are rendered on the server (the data is in `__NEXT_DATA__`), so the
  careers site doesn't load it either. It fits no tier of ground rule 1, so it isn't read.
- Slugs are case-sensitive (`Rippling` is a 404), and every slug seen is lowercase, so tokens
  are sent as given.
- Job pages also work with a locale (`ats.rippling.com/en-GB/{slug}/jobs/{id}`) and with
  `/apply` after the id. `ats.rippling.com/rippling/jobs` redirects to Rippling's own careers
  site, but the API slug works the same.
- Empty board (`demo`): 200 with `items: []` and `totalPages: 0`. Unknown board: **404**
  `{"error_code":"RESOURCE_NOT_FOUND","message":"Job Board not found","resource":null}`.

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

### Oracle Recruiting Cloud (verified 2026-10-17; tier 2, undocumented)

Oracle's Candidate Experience careers sites (`{pod}.fa.{region}.oraclecloud.com/hcmUI/
CandidateExperience/...`) load jobs from HCM REST resources, with no sign-in. Oracle documents
the HCM REST API for its customers, not this public use, so it's tier 2. Checked against City of
Atlanta (`ehxr.fa.us2`, `CX_1`) and Oracle (`eeho.fa.us2`, `CX_45001`).

- **robots.txt** is 404 on these hosts, which means no rules.
- **Search:** `GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&
  expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber=CX_1,limit=200,offset=0,
  sortBy=POSTING_DATES_DESC[,keyword="..."]`. The answer is `items[0]` with `TotalJobsCount` and
  `requisitionList`. `limit` is capped at 200 (asking for 500 returns 200).
- **An empty `keyword=""` finds nothing.** Leave the keyword out to list the whole site.
- **An unknown site number isn't an error:** `siteNumber=CX_999` returns the tenant's default
  site. A missing pod times out (no DNS answer).
- **Rows** have `Id` (numeric), `Title`, `PostedDate` (`YYYY-MM-DD`), `PrimaryLocation`,
  `PrimaryLocationCountry` (ISO alpha-2), `secondaryLocations`, `WorkplaceType` /
  `WorkplaceTypeCode` (blank on both sites checked), and no description.
- **Detail:** `GET .../recruitingCEJobRequisitionDetails?expand=all&onlyData=true&
  finder=ById;Id="34378",siteNumber=CX_1`. The posting text is `ExternalDescriptionStr`,
  `ExternalResponsibilitiesStr`, and `ExternalQualificationsStr` (HTML; Atlanta puts the salary
  range there). `Internal*` twins exist and aren't read. `ExternalPostedStartDate` is an ISO
  timestamp. An id that isn't posted returns 200 with `items: []`.
- **Tokens** are `{pod}.fa.{region}/{site number}`, lowercased in job ids; site numbers are sent
  uppercase (`CX_1`). Regions seen in the openings-mcp roster: `ocs`, `us2`, `us6`, `us8`, `em2`,
  `em3`, `em5`, `em8`, `ap1`, `ca2`.
