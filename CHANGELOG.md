# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `sweep_boards` and `get_posting` for Greenhouse, Ashby, and Lever boards, with pay, posted
  date, and remote status normalized in code.
- `find_company_posting`, `resolve_board`, and `is_posting_live`.
- `list_watchlist`, `add_company`, and `update_company`, plus `WATCHLIST_READONLY` to turn the
  write tools off.
- `ALLOW_CUSTOM_URLS=false` refuses careers URLs from the client, so a public server fetches
  careers pages only from its own watchlist.
- An example watchlist, copied on first run to a per-user watchlist:
  `%APPDATA%\jobboard-mcp\watchlist.yaml` on Windows, `~/.config/jobboard-mcp/watchlist.yaml`
  elsewhere (`$XDG_CONFIG_HOME` is honored). `list_watchlist` returns its `path`.
- Archiving: `update_company(archived=true)` keeps a company on the watchlist but stops
  sweeping it. `list_watchlist` hides archived companies unless `include_archived` is set.
- `sweep_boards` takes `limit` (default 100, at most 250) and reports `matched` and `truncated`.
  Passing the returned ids in `exclude_ids` gets the next page.
- `unverified_filters` on each swept job names the filters it was kept under without being
  checked (`remote`, `min_pay`, `posted_within_days`) because the data was missing or the pay
  was in another currency. `unverified_counts` totals them.
- `sweep_boards` returns `term_hits`: how many titles on the swept boards each title term
  matched, before filtering. A 0 flags a term that matches nothing.
- `find_company_posting` returns `watchlist_match` (`name`, `loose_name`, `board`, or null).

### Changed

- `sweep_boards` has no built-in filters. `title_exclude` used to default to intern, internship,
  new grad, and principal, and `remote` to `remote_us` and `remote_global`. Both now default to
  no filtering, so pass your preferences on each call.
- `pay_interval` is now `pay_posted_interval`, so it isn't read as the unit of `pay_min` and
  `pay_max`. Those are always annual.
- Locations that name only non-US countries or regions ("Portugal", "ANZ Region", "EMEA") are
  `remote_other` instead of `unknown`, so they no longer show up in US-remote sweeps.
- A bracketed remote hint in a title ("(US Remote)", "(remote)") counts as remote evidence when
  the ATS gives no workplace type.
- `employment_type` is `contract` when the title marks the role as one ("(Contract)",
  "Contract Software Engineer").
- `flags` describe the job, not the search: `pay_unknown` and `date_unknown` appear whenever
  the data is missing, not only when a sweep filters on it, and every tool shows the same
  flags for a job.
- Pay in any currency now has numbers. `pay_min` and `pay_max` are annual amounts in
  `pay_currency` (USD when the posting offers it, otherwise the first currency posted), and
  other currencies the posting lists are in the new `pay_other_currencies`. Nothing is
  converted. Non-USD pay used to have null numbers and count as `pay_unknown`.
- `min_pay` takes a `min_pay_currency` (default `USD`) and compares against the job's range in
  that currency. Jobs with no range in that currency are kept.
- Posting links are upgraded from `http://` to `https://`.
- Title terms are plain phrases only. A term starting with `re:` or using regex characters
  (`| * ? ( ) [ ] { } ^ $ \`) is refused with an error before any board is fetched. Regex
  terms were dropped earlier and had been matching nothing without saying so.

### Fixed

- Pay written with dots as thousands separators (`€68.000–€91.000`) was read as hourly.
- `find_company_posting` found a watched company only by its exact name, so "Temporal" missed
  "Temporal Technologies", got a guessed board, and gave its jobs different fingerprints than
  `sweep_boards`. It now matches names loosely, maps a resolved board back to its watchlist
  entry, and returns the watchlist's name.

### Security

- Careers-page fetches (`resolve_board`, `find_company_posting`) only go to public addresses
  over `http`/`https` on the default port. Every redirect is checked, and the connection is
  pinned to the checked address so DNS rebinding can't get around it.
