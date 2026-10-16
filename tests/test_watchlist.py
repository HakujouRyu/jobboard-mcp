from pathlib import Path

import pytest

from jobboard_mcp.models import Ats, WatchlistEntry
from jobboard_mcp.watchlist import (
    WatchlistError,
    WatchlistStore,
    load_watchlist,
    parse_watchlist,
)

REPO_WATCHLIST = Path(__file__).resolve().parents[1] / "data" / "watchlist.yaml"

SAMPLE = """\
# Header comment that must survive edits.

- company: GitLab
  ats: greenhouse
  token: gitlab
  domains: [dev tools, open source]
  tags: [hww]
  notes: >-
    A folded note that
    spans two lines.

- company: Posit
  careers_url: https://posit.co/careers/
  domains: [open source]
  tags: [hww]
  interview_note: Take-home project

# Dropped on review:
#   Replit: every role was hybrid.
"""


@pytest.fixture
def store(tmp_path: Path) -> WatchlistStore:
    path = tmp_path / "watchlist.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    return WatchlistStore(path)


def test_repo_watchlist_is_valid() -> None:
    entries = load_watchlist(REPO_WATCHLIST)
    assert len(entries) >= 20
    assert {"GitLab", "Linear", "Temporal Technologies", "Posit"} <= {e.company for e in entries}


def test_parse_errors() -> None:
    with pytest.raises(WatchlistError, match="duplicate"):
        parse_watchlist("- company: A\n- company: a\n")
    with pytest.raises(WatchlistError, match="both ats and token"):
        parse_watchlist("- company: A\n  ats: lever\n")
    with pytest.raises(WatchlistError, match="expected a list"):
        parse_watchlist("company: A\n")
    with pytest.raises(WatchlistError):
        parse_watchlist("- company: A\n  ats: workday\n  token: x\n")
    assert parse_watchlist("") == []


def test_add_appends_and_preserves_comments(store: WatchlistStore) -> None:
    store.add(
        WatchlistEntry(company="Render", ats=Ats.ASHBY, token="render", domains=["cloud infra"])
    )
    text = store.path.read_text(encoding="utf-8")
    assert text.startswith("# Header comment that must survive edits.")
    assert "#   Replit: every role was hybrid." in text
    assert "notes: >-\n    A folded note that\n    spans two lines." in text
    assert "\n\n- company: Render\n  ats: ashby\n  token: render\n" in text
    assert "domains: [cloud infra]" in text
    assert "careers_url: null" in text
    assert [e.company for e in store.load()] == ["GitLab", "Posit", "Render"]


def test_add_rejects_duplicates(store: WatchlistStore) -> None:
    with pytest.raises(WatchlistError, match="already"):
        store.add(WatchlistEntry(company="gitlab"))


def test_update_fills_board_in_field_order(store: WatchlistStore) -> None:
    updated = store.update("posit", {"ats": Ats.GREENHOUSE, "token": "posit"})
    assert (updated.ats, updated.token) == (Ats.GREENHOUSE, "posit")
    text = store.path.read_text(encoding="utf-8")
    assert "- company: Posit\n  ats: greenhouse\n  token: posit\n  careers_url:" in text
    assert "# Header comment" in text and "#   Replit" in text
    assert store.find("Posit") == updated


def test_update_validates(store: WatchlistStore) -> None:
    with pytest.raises(WatchlistError, match="unknown watchlist fields"):
        store.update("GitLab", {"color": "blue"})
    with pytest.raises(WatchlistError, match="not on the watchlist"):
        store.update("Nobody", {"notes": "x"})
    with pytest.raises(WatchlistError, match="both ats and token"):
        store.update("Posit", {"ats": Ats.LEVER})
    with pytest.raises(WatchlistError, match="duplicate"):
        store.update("Posit", {"company": "GitLab"})
    # Nothing was written by the failed updates.
    assert store.path.read_text(encoding="utf-8") == SAMPLE


def test_readonly(tmp_path: Path) -> None:
    path = tmp_path / "w.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    ro = WatchlistStore(path, readonly=True)
    assert len(ro.load()) == 2
    with pytest.raises(WatchlistError, match="read-only"):
        ro.add(WatchlistEntry(company="X"))
    with pytest.raises(WatchlistError, match="read-only"):
        ro.update("GitLab", {"notes": "x"})


def test_add_to_missing_file(tmp_path: Path) -> None:
    store = WatchlistStore(tmp_path / "new" / "w.yaml")
    store.add(WatchlistEntry(company="A"))
    assert [e.company for e in store.load()] == ["A"]
