"""Rebuild src/jobboard_mcp/boards_greenhouse.tsv from ats-jobs-mcp's board directory.

Usage:
    uv run python scripts/build_board_directory.py path/to/directory.json COMMIT_SHA

Download ``src/ats_jobs_mcp/directory.json`` from
https://github.com/groundtruthtools/ats-jobs-mcp at that commit first. Only Greenhouse boards
with a company name are kept: an Ashby row has no name, so it adds nothing to the token guesses
resolve_board already makes. The output is sorted, so rebuilding from the same input gives the
same file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src" / "jobboard_mcp" / "boards_greenhouse.tsv"
SOURCE = "https://github.com/groundtruthtools/ats-jobs-mcp"


def main(source: Path, commit: str) -> None:
    boards = json.loads(source.read_text(encoding="utf-8"))["boards"]
    rows: set[tuple[str, str]] = set()
    checked: set[str] = set()
    for row in boards:
        company, token = row.get("company"), row.get("board")
        if row.get("ats") != "greenhouse" or not company or not token:
            continue
        company, token = " ".join(company.split()), token.strip()
        if "\t" in company + token or not company or not token:
            continue
        rows.add((company, token))
        checked.add(row["checked"])
    lines = [
        "# Greenhouse boards by company name, for resolve_board (src/jobboard_mcp/directory.py).",
        f"# From {SOURCE}, src/ats_jobs_mcp/directory.json at commit {commit}.",
        "# MIT License, Copyright (c) 2026 Ground Truth. See THIRD_PARTY_NOTICES.md.",
        "# Rebuild: uv run python scripts/build_board_directory.py <directory.json> <commit>",
        f"# checked: {max(checked)}",
        *(f"{c}\t{t}" for c, t in sorted(rows, key=lambda r: (r[0].casefold(), r[1]))),
    ]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {len(rows)} boards to {OUT}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), sys.argv[2])
