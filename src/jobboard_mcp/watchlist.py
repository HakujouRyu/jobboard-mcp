"""Load, validate, and edit the watchlist file.

Writes go through ruamel.yaml's round-trip mode so comments and formatting survive, and land
atomically (temp file + replace). The watchlist is the only thing this server ever writes.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import FoldedScalarString, LiteralScalarString

from jobboard_mcp.models import WatchlistEntry

_ENTRIES = TypeAdapter(list[WatchlistEntry])
# Field order for new entries, matching the existing file.
_FIELDS = list(WatchlistEntry.model_fields)
_LIST_FIELDS = {"domains", "tags"}
_BLOCK_SCALARS = (FoldedScalarString, LiteralScalarString)


class WatchlistError(ValueError):
    pass


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=2, offset=0)
    y.width = 100
    y.representer.add_representer(
        type(None), lambda r, _d: r.represent_scalar("tag:yaml.org,2002:null", "null")
    )
    return y


def _validate(doc: Any, source: str) -> list[WatchlistEntry]:
    if doc is None:
        return []
    if not isinstance(doc, list):
        raise WatchlistError(f"{source}: expected a list of companies")
    try:
        entries = _ENTRIES.validate_python([dict(item) for item in doc])
    except (TypeError, ValueError, ValidationError) as exc:
        raise WatchlistError(f"{source}: {exc}") from exc
    seen: set[str] = set()
    for e in entries:
        key = e.company.casefold()
        if key in seen:
            raise WatchlistError(f"{source}: duplicate company {e.company!r}")
        seen.add(key)
        if (e.ats is None) != (e.token is None):
            raise WatchlistError(f"{source}: {e.company!r} needs both ats and token, or neither")
    return entries


def parse_watchlist(text: str, source: str = "<string>") -> list[WatchlistEntry]:
    try:
        doc = _yaml().load(text)
    except YAMLError as exc:
        raise WatchlistError(f"{source}: invalid YAML: {exc}") from exc
    return _validate(doc, source)


def load_watchlist(path: Path) -> list[WatchlistEntry]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WatchlistError(f"watchlist not found: {path}") from exc
    return parse_watchlist(text, str(path))


def seed_watchlist(path: Path, example: Path) -> bool:
    """Copy ``example`` to ``path`` if nothing is there yet. Returns whether it copied."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, path)
    return True


def _plain(entry: WatchlistEntry) -> CommentedMap:
    data = entry.model_dump(mode="json")
    out = CommentedMap()
    for key in _FIELDS:
        value = data[key]
        if key in _LIST_FIELDS:
            seq = CommentedSeq(value)
            seq.fa.set_flow_style()
            value = seq
        out[key] = value
    return out


def _move_trailing(item: CommentedMap, src: str, dst: str) -> None:
    """Move the blank line (or comment block) after an entry from one key to another.

    ruamel attaches it to the entry's last key. After a plain value the comment text starts with
    that line's own newline; a block scalar (``notes: >-``) ends its own line, so the text has
    one newline fewer. Moving between the two kinds adds or drops that newline.
    """
    trailing = item.ca.items.pop(src, None)
    if trailing is None or trailing[2] is None:
        if trailing is not None:
            item.ca.items[dst] = trailing
        return
    src_block = isinstance(item.get(src), _BLOCK_SCALARS)
    dst_block = isinstance(item.get(dst), _BLOCK_SCALARS)
    token = trailing[2]
    if src_block and not dst_block:
        token.value = "\n" + token.value
    elif dst_block and not src_block and token.value.startswith("\n"):
        token.value = token.value[1:]
    item.ca.items[dst] = trailing


def _remove_key(item: CommentedMap, key: str) -> None:
    """Delete a key, keeping the blank line or comments that follow the entry."""
    was_last = next(reversed(item), None) == key
    keys = list(item)
    if was_last and len(keys) > 1:
        _move_trailing(item, key, keys[-2])
    item.ca.items.pop(key, None)
    del item[key]


class WatchlistStore:
    """Read and edit the watchlist file. Edits re-read the file first, so manual edits made
    while the server runs are never overwritten."""

    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = path
        self.readonly = readonly

    def load(self) -> list[WatchlistEntry]:
        return load_watchlist(self.path)

    def find(self, company: str) -> WatchlistEntry | None:
        key = company.strip().casefold()
        return next((e for e in self.load() if e.company.casefold() == key), None)

    def _check_writable(self) -> None:
        if self.readonly:
            raise WatchlistError("watchlist is read-only on this server (WATCHLIST_READONLY)")

    def _read_doc(self) -> tuple[YAML, Any]:
        y = _yaml()
        try:
            doc = y.load(self.path.read_text(encoding="utf-8")) if self.path.exists() else None
        except YAMLError as exc:
            raise WatchlistError(f"{self.path}: invalid YAML: {exc}") from exc
        if doc is None:
            doc = CommentedSeq()
        _validate(doc, str(self.path))
        return y, doc

    def _write_doc(self, y: YAML, doc: Any) -> None:
        buf = io.StringIO()
        y.dump(doc, buf)
        text = buf.getvalue()
        _validate(_yaml().load(text), str(self.path))  # never write a file we can't read back
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".watchlist-", suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def add(self, entry: WatchlistEntry) -> WatchlistEntry:
        self._check_writable()
        y, doc = self._read_doc()
        if any(str(item.get("company", "")).casefold() == entry.company.casefold() for item in doc):
            raise WatchlistError(f"{entry.company!r} is already on the watchlist")
        plain = _plain(entry)
        if not entry.archived:
            del plain["archived"]  # only archived entries carry the key
        doc.append(plain)
        if len(doc) > 1:
            doc.yaml_set_comment_before_after_key(len(doc) - 1, before="\n", indent=0)
        self._write_doc(y, doc)
        return entry

    def update(self, company: str, changes: Mapping[str, Any]) -> WatchlistEntry:
        """Apply a partial update. Keys must be WatchlistEntry fields; renaming is allowed."""
        self._check_writable()
        unknown = set(changes) - set(_FIELDS)
        if unknown:
            raise WatchlistError(f"unknown watchlist fields: {sorted(unknown)}")
        y, doc = self._read_doc()
        key = company.strip().casefold()
        item = next((i for i in doc if str(i.get("company", "")).casefold() == key), None)
        if item is None:
            raise WatchlistError(f"{company!r} is not on the watchlist")
        merged = WatchlistEntry.model_validate({**dict(item), **changes})
        fresh = _plain(merged)
        for field in _FIELDS:
            if field not in changes:
                continue
            value = fresh[field]
            if field == "archived" and not value:
                if field in item:
                    _remove_key(item, field)  # unarchived entries don't carry the key
            elif field in item:
                item[field] = value
            elif value not in (None, []):
                # Keep the usual field order: insert after the fields that precede it.
                earlier = set(_FIELDS[: _FIELDS.index(field)])
                pos = sum(1 for k in item if k in earlier)
                last = next(reversed(item), None)
                item.insert(pos, field, value)
                if pos == len(item) - 1 and last is not None:
                    # Keep the blank line after the entry instead of splitting the entry.
                    _move_trailing(item, last, field)
        self._write_doc(y, doc)
        return merged
