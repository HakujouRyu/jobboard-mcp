## What and why

<!-- What changed, and why. Link the issue if there is one. -->

## How it was tested

<!-- Tests added or changed. If you touched an adapter, http.py, or resolve.py, say whether you
ran `uv run pytest -m live` and paste the relevant output. -->

## Checklist

- [ ] `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pyright` pass
- [ ] Follows the ground rules in CONTRIBUTING.md (public JSON APIs only, never guess `remote_us`, flag rather than drop, nothing on stdout)
- [ ] `docs/tools.md` and the README are updated if a tool's arguments or output changed
- [ ] `CHANGELOG.md` has an entry under Unreleased if users will notice the change
