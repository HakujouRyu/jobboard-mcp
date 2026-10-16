# ATS API notes (verified 2026-10-15)

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
| `*_404.*` | unknown-token responses |

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
