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
