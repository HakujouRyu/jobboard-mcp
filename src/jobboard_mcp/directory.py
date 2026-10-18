"""Known Greenhouse boards by company name, so resolve_board can find a board whose token
doesn't follow from the name (Addepar is ``addepar1``, Abnormal is ``abnormalsecurity``).

The list ships with the package (``boards_greenhouse.tsv``, from ats-jobs-mcp; see
THIRD_PARTY_NOTICES.md). Each board was confirmed against the Greenhouse API on the date in the
file's header, and resolve_board still probes a listed board live before it reports it.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

from jobboard_mcp.matching import match_company
from jobboard_mcp.normalize import normalize_text

BOARDS_FILE = Path(__file__).with_name("boards_greenhouse.tsv")
MAX_LISTINGS = 3


@dataclass(frozen=True)
class Listing:
    company: str  # as the directory names it
    token: str


@dataclass(frozen=True)
class Directory:
    by_name: dict[str, list[Listing]]  # normalized name -> listings
    names: list[str]  # every listed company name, for loose matching
    checked: str  # when the boards were confirmed (YYYY-MM-DD)

    def lookup(self, company: str) -> list[Listing]:
        """Listings whose name equals ``company`` after normalizing, else the one listed name
        that starts with it word for word (or that it starts with). Never guesses between
        several loose matches."""
        exact = self.by_name.get(normalize_text(company), [])
        if exact:
            return exact[:MAX_LISTINGS]
        name = match_company(company, self.names)
        return self.by_name[normalize_text(name)][:MAX_LISTINGS] if name else []


def parse_directory(text: str) -> Directory:
    by_name: dict[str, list[Listing]] = {}
    checked = ""
    for line in text.splitlines():
        if line.startswith("# checked:"):
            checked = line.split(":", 1)[1].strip()
            continue
        if not line or line.startswith("#"):
            continue
        company, sep, token = line.partition("\t")
        if not sep or not company or not token:
            continue
        by_name.setdefault(normalize_text(company), []).append(Listing(company, token))
    names = [listings[0].company for listings in by_name.values()]
    return Directory(by_name=by_name, names=names, checked=checked)


@functools.cache
def directory() -> Directory:
    return parse_directory(BOARDS_FILE.read_text(encoding="utf-8"))
