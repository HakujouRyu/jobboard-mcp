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
- An example watchlist, copied to `data/watchlist.yaml` on first run.

### Changed

- `pay_interval` is now `pay_posted_interval`, so it isn't read as the unit of `pay_min` and
  `pay_max`. Those are always annual.
- Locations that name only non-US countries or regions ("Portugal", "ANZ Region", "EMEA") are
  `remote_other` instead of `unknown`, so they no longer show up in US-remote sweeps.
- A bracketed remote hint in a title ("(US Remote)", "(remote)") counts as remote evidence when
  the ATS gives no workplace type.
- `employment_type` is `contract` when the title marks the role as one ("(Contract)",
  "Contract Software Engineer").

### Fixed

- Pay written with dots as thousands separators (`€68.000–€91.000`) was read as hourly.

### Security

- Careers-page fetches (`resolve_board`, `find_company_posting`) only go to public addresses
  over `http`/`https` on the default port. Every redirect is checked, and the connection is
  pinned to the checked address so DNS rebinding can't get around it.
