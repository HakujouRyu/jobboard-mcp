"""Load and validate data/watchlist.yaml (read-only in M1)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import TypeAdapter

from jobboard_mcp.models import WatchlistEntry

_ENTRIES = TypeAdapter(list[WatchlistEntry])


class WatchlistError(ValueError):
    pass


def parse_watchlist(text: str, source: str = "<string>") -> list[WatchlistEntry]:
    data = yaml.safe_load(text) or []
    if not isinstance(data, list):
        raise WatchlistError(f"{source}: expected a list of companies")
    entries = _ENTRIES.validate_python(data)
    seen: set[str] = set()
    for e in entries:
        key = e.company.casefold()
        if key in seen:
            raise WatchlistError(f"{source}: duplicate company {e.company!r}")
        seen.add(key)
        if (e.ats is None) != (e.token is None):
            raise WatchlistError(f"{source}: {e.company!r} needs both ats and token, or neither")
    return entries


def load_watchlist(path: Path) -> list[WatchlistEntry]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WatchlistError(f"watchlist not found: {path}") from exc
    return parse_watchlist(text, str(path))
