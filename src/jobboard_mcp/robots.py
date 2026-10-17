"""Parse robots.txt and decide whether a path may be fetched, following RFC 9309. Pure: no I/O.

- The group for our product token is used, else the ``*`` group, else everything is allowed.
  Several groups for the same agent are merged.
- The longest matching rule wins, and ``allow`` wins a tie. No match means allowed.
- ``*`` matches any run of characters and a trailing ``$`` anchors the end. Matching uses
  ``str.find``, never a regex built from the file, so a hostile file can't make it slow.
- Only the first ``MAX_BYTES`` are read, as the RFC allows. Other keys (``crawl-delay``,
  ``sitemap``) are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

MAX_BYTES = 500 * 1024

_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
# Kept as-is when normalizing: reserved characters, existing escapes, and the wildcards.
_SAFE = "!#$%&'()*+,/:;=?@[]~"
_TOKEN = re.compile(r"[A-Za-z_-]*")


def _normalize(text: str) -> str:
    """Percent-encode what needs it and decode escapes of unreserved characters, so
    ``/%7Ejobs``, ``/~jobs``, and ``/%7ejobs`` all compare equal."""

    def unescape(m: re.Match[str]) -> str:
        char = chr(int(m[1], 16))
        return char if char in _UNRESERVED else m[0].upper()

    return _ESCAPE.sub(unescape, quote(text, safe=_SAFE))


def _matches(pattern: str, path: str) -> bool:
    anchored = pattern.endswith("$")
    parts = (pattern[:-1] if anchored else pattern).split("*")
    if not path.startswith(parts[0]):
        return False
    pos = len(parts[0])
    if len(parts) == 1:
        return not anchored or pos == len(path)
    for part in parts[1:-1]:
        found = path.find(part, pos)
        if found < 0:
            return False
        pos = found + len(part)
    last = parts[-1]
    if anchored:
        return len(path) - len(last) >= pos and path.endswith(last)
    return path.find(last, pos) >= 0


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str  # normalized


@dataclass(frozen=True)
class RobotsRules:
    rules: tuple[Rule, ...] = ()

    def allows(self, path: str) -> bool:
        """``path`` is the URL's path plus any query, as sent (``/jobs?page=2``)."""
        target = _normalize(path or "/")
        best: Rule | None = None
        for rule in self.rules:
            if not _matches(rule.pattern, target):
                continue
            if (
                best is None
                or len(rule.pattern) > len(best.pattern)
                or (len(rule.pattern) == len(best.pattern) and rule.allow)
            ):
                best = rule
        return best is None or best.allow


ALLOW_ALL = RobotsRules()


def _agent(value: str) -> str:
    """The product token at the start of a user-agent value, lowercased ("Bot/2.1" -> "bot")."""
    value = value.strip()
    if value.startswith("*"):
        return "*"
    match = _TOKEN.match(value)
    return match[0].lower() if match else ""


def parse_robots(content: bytes, product: str) -> RobotsRules:
    """The rules that apply to ``product`` (a product token such as ``jobboard-mcp``)."""
    text = (
        content[:MAX_BYTES]
        .decode("utf-8", errors="replace")
        .removeprefix("\N{ZERO WIDTH NO-BREAK SPACE}")
    )
    groups: list[tuple[set[str], list[Rule]]] = []
    agents: set[str] = set()
    rules: list[Rule] = []
    in_rules = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if in_rules:  # a user-agent line after rules starts a new group
                groups.append((agents, rules))
                agents, rules, in_rules = set(), [], False
            agents.add(_agent(value))
        elif key in ("allow", "disallow"):
            in_rules = True
            if not agents or not value:  # rules before any group, or an empty rule
                continue
            if not value.startswith(("/", "*")):
                value = "/" + value
            rules.append(Rule(allow=key == "allow", pattern=_normalize(value)))
    if agents:
        groups.append((agents, rules))

    product = product.lower()
    for wanted in (product, "*"):
        matched = [rule for names, group in groups if wanted in names for rule in group]
        if any(wanted in names for names, _ in groups):
            return RobotsRules(tuple(matched))
    return ALLOW_ALL
