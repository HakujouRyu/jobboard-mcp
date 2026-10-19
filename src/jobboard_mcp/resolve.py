"""Find which ATS board a company uses (SPEC §5.4). Read-only: never edits the watchlist.

Three sources of evidence, combined:
1. Probe likely tokens (from the name and the careers-page domain) against each list endpoint.
2. Probe the Greenhouse boards listed for the name in the bundled directory (``directory.py``),
   which covers tokens that don't follow from the name.
3. Fetch the careers page, plus up to two same-site careers/jobs links from it, and look for
   links or embeds pointing at a Greenhouse, Ashby, Lever, Workable, Rippling, Personio,
   Workday, or Oracle board.

Nothing else is scraped: pages are only scanned for board URLs.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from rapidfuzz import fuzz

from jobboard_mcp.adapters import ADAPTERS
from jobboard_mcp.directory import directory
from jobboard_mcp.http import Blocked, FetchError, HttpClient, TextPage
from jobboard_mcp.models import Ats, BoardCandidate, Confidence, Resolution
from jobboard_mcp.normalize import normalize_text

MAX_TOKENS = 6
# Systems whose boards are probed with guessed tokens; the rest are found from careers pages.
_GUESSABLE = [ats for ats in Ats if ADAPTERS[ats].guess_tokens]
MAX_EXTRA_PAGES = 2

# Words dropped from a company name to get its core ("Flower Labs" -> "flower").
_NAME_SUFFIXES = {
    "inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "group", "hq",
    "labs", "lab", "technologies", "technology", "tech", "software", "systems", "foundation",
    "pbc", "gmbh", "ag", "sa", "the", "io", "ai", "app", "apps", "and",
}  # fmt: skip
_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._-]*"
# Board references in page source. Each pattern's first group is the token.
_BOARD_PATTERNS: list[tuple[Ats, re.Pattern[str]]] = [
    (Ats.GREENHOUSE, re.compile(rf"greenhouse\.io/embed/job_board(?:/js)?\?(?:[^\"'\s<>]*?&(?:amp;)?)?for=({_TOKEN})", re.I)),  # noqa: E501
    (Ats.GREENHOUSE, re.compile(rf"boards-api\.greenhouse\.io/v1/boards/({_TOKEN})", re.I)),
    (Ats.GREENHOUSE, re.compile(rf"(?:boards|job-boards(?:\.eu)?)\.greenhouse\.io/(?!embed\b)({_TOKEN})", re.I)),  # noqa: E501
    (Ats.ASHBY, re.compile(rf"api\.ashbyhq\.com/posting-api/job-board/({_TOKEN})", re.I)),
    (Ats.ASHBY, re.compile(rf"jobs\.ashbyhq\.com/({_TOKEN})", re.I)),
    (Ats.LEVER, re.compile(rf"api\.(?:eu\.)?lever\.co/v0/postings/({_TOKEN})", re.I)),
    (Ats.LEVER, re.compile(rf"jobs\.(?:eu\.)?lever\.co/({_TOKEN})", re.I)),
    (Ats.WORKABLE, re.compile(rf"(?:www\.)?workable\.com/api/(?:v\d/)?(?:widget/)?accounts/({_TOKEN})", re.I)),  # noqa: E501
    (Ats.WORKABLE, re.compile(rf"apply\.workable\.com/(?!api/|j/)({_TOKEN})", re.I)),
    (Ats.RIPPLING, re.compile(rf"api\.rippling\.com/platform/api/ats/v\d/board/({_TOKEN})", re.I)),
    (Ats.RIPPLING, re.compile(rf"ats\.rippling\.com/(?:[a-z]{{2}}-[a-z]{{2}}/)?(?!api/|internal/)({_TOKEN})", re.I)),  # noqa: E501
    # The token is the subdomain. The lookbehind starts each match at the beginning of a
    # label run, so a long run of letters is scanned once, not from every position.
    (Ats.PERSONIO, re.compile(r"(?<![A-Za-z0-9.-])([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)\.jobs\.personio\.(?:de|com)\b", re.I)),  # noqa: E501
]  # fmt: skip
# Unsupported systems worth naming in the evidence.
_OTHER_ATS = re.compile(
    r"\b(smartrecruiters\.com|recruitee\.com|bamboohr\.com|"
    r"jobvite\.com|breezy\.hr|teamtailor\.com|pinpointhq\.com|"
    r"icims\.com|phenompeople\.com|successfactors\.\w+|taleo\.net|wellfound\.com)",
    re.I,
)
# A Workday careers site: {tenant}.wdN.myworkdayjobs.com/{site}, with an optional locale.
# The lookbehind (not \b, which also matches after each hyphen) starts a match only at the
# beginning of a label run, so a long run like "a-a-a-..." is scanned once, not from every
# letter in it (ground rule 9). Likewise for _ORACLE.
_WORKDAY = re.compile(
    r"(?<![a-z0-9_-])([a-z0-9][a-z0-9-]*)\.(wd\d{1,3})\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)",
    re.I,
)
# An Oracle Recruiting Cloud site: {pod}.fa.{region}.oraclecloud.com/hcmUI/CandidateExperience/
# {lang}/sites/{site}.
_ORACLE = re.compile(
    r"(?<![a-z0-9_-])([a-z0-9][a-z0-9-]*)\.fa\.([a-z0-9]+)\.oraclecloud\.com/"
    r"hcmUI/CandidateExperience/"
    r"(?:[a-z]{2}(?:-[a-z]{2})?/)?sites/([\w-]+)",
    re.I,
)
_HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)
_CAREER_PATH = re.compile(r"career|jobs?\b|join|work-with-us|hiring|openings|positions", re.I)


def token_candidates(company: str | None, careers_url: str | None = None) -> list[str]:
    """Likely board tokens: the slugified name and simple variants, plus the site's domain."""
    out: list[str] = []
    if company:
        words = normalize_text(company).split()
        core = [w for w in words if w not in _NAME_SUFFIXES] or words[:1]
        out += ["".join(words), "-".join(words), "".join(core), "-".join(core), core[0]]
    if careers_url:
        host = urlsplit(careers_url if "//" in careers_url else f"//{careers_url}").hostname or ""
        labels = [p for p in host.lower().split(".") if p not in ("www", "careers", "jobs")]
        if len(labels) >= 2:
            out.append(re.sub(r"[^a-z0-9-]", "", labels[-2]))
    return [t for t in dict.fromkeys(out) if t][:MAX_TOKENS]


def find_board_refs(html: str) -> list[tuple[Ats, str]]:
    """(ats, token) pairs referenced in a page, most-referenced first. Tokens are lowercased
    (every supported ATS treats them case-insensitively)."""
    text = html.replace("\\/", "/").replace("%2F", "/")
    counts: dict[tuple[Ats, str], int] = {}
    for ats, pattern in _BOARD_PATTERNS:
        for m in pattern.finditer(text):
            token = m.group(1).rstrip(".-_").lower()
            if token in ("embed", "api", "v1", "jobs", "posting-api", "j"):
                continue
            counts[(ats, token)] = counts.get((ats, token), 0) + 1
    for m in _WORKDAY.finditer(text):
        if m.group(3).lower() in ("wday", "job", "jobs"):
            continue
        token = f"{m.group(1)}.{m.group(2)}/{m.group(3)}".lower()
        counts[(Ats.WORKDAY, token)] = counts.get((Ats.WORKDAY, token), 0) + 1
    for m in _ORACLE.finditer(text):
        token = f"{m.group(1)}.fa.{m.group(2)}/{m.group(3)}".lower()
        counts[(Ats.ORACLE, token)] = counts.get((Ats.ORACLE, token), 0) + 1
    return sorted(counts, key=lambda k: -counts[k])


def other_ats_hosts(html: str) -> list[str]:
    return sorted({m.group(1).lower() for m in _OTHER_ATS.finditer(html)})


def career_links(page: TextPage, limit: int = MAX_EXTRA_PAGES) -> list[str]:
    """Same-site links that look like careers or jobs pages, best first."""
    base = urlsplit(page.url)
    site = (base.hostname or "").removeprefix("www.")
    seen: dict[str, int] = {}
    for href in _HREF.findall(page.text):
        url = urljoin(page.url, href.strip())
        parts = urlsplit(url)
        host = (parts.hostname or "").removeprefix("www.")
        if parts.scheme not in ("http", "https") or not host.endswith(site):
            continue
        if not _CAREER_PATH.search(parts.path) or url.rstrip("/") == page.url.rstrip("/"):
            continue
        # Prefer paths that say careers/jobs outright, then shorter paths.
        rank = (0 if re.search(r"career|jobs?\b", parts.path, re.I) else 10) + parts.path.count("/")
        seen.setdefault(url.split("?")[0], rank)
    return sorted(seen, key=lambda u: seen[u])[:limit]


def _name_matches(company: str | None, names: set[str]) -> bool:
    if not company:
        return False
    target = normalize_text(company)
    return any(fuzz.token_set_ratio(target, normalize_text(n)) >= 90 for n in names)


@dataclass
class _Found:
    ats: Ats
    token: str
    jobs: int
    on_page: bool = False
    name_match: bool = False
    evidence: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> Confidence:
        if self.on_page or self.name_match:
            return Confidence.HIGH
        return Confidence.MEDIUM if self.jobs else Confidence.LOW

    @property
    def rank(self) -> tuple[int, int, int]:
        order = {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}
        return (order[self.confidence], 0 if self.on_page else 1, -self.jobs)


async def _probe(
    http: HttpClient, company: str | None, ats: Ats, token: str, refresh: bool
) -> _Found | None:
    try:
        board = await ADAPTERS[ats].probe(http, token, refresh=refresh)
    except FetchError:
        return None
    names = board.names
    found = _Found(ats, token, board.jobs, name_match=_name_matches(company, names))
    if found.name_match:
        found.evidence.append(f"board reports company name {sorted(names)[0]!r}")
    return found


async def _scan_pages(
    http: HttpClient, careers_url: str, refresh: bool
) -> tuple[list[tuple[Ats, str, str]], list[str], list[str]]:
    """Board refs (ats, token, page), other-ATS hosts, and pages checked."""
    refs: list[tuple[Ats, str, str]] = []
    others: list[str] = []
    checked: list[str] = []
    queue = [careers_url]
    while queue and len(checked) < 1 + MAX_EXTRA_PAGES:
        url = queue.pop(0)
        try:
            page: TextPage = (await http.get_text(url, refresh=refresh)).data
        except Blocked as exc:
            checked.append(f"{url} ({exc})")
            continue
        except FetchError:
            checked.append(url)
            continue
        checked.append(page.url if page.url == url else f"{url} -> {page.url}")
        refs += [(a, t, page.url) for a, t in find_board_refs(page.text)]
        others += other_ats_hosts(page.text)
        if refs:
            break
        if len(checked) == 1:  # follow links from the first page only
            queue += career_links(page)
    return refs, sorted(set(others)), checked


async def resolve_board(
    http: HttpClient,
    company: str | None = None,
    careers_url: str | None = None,
    *,
    refresh: bool = False,
) -> Resolution:
    if not company and not careers_url:
        raise ValueError("pass company and/or careers_url")
    tokens = token_candidates(company, careers_url)
    board_dir = directory()
    listed = board_dir.lookup(company) if company else []
    listed_tokens = [t for t in dict.fromkeys(x.token.lower() for x in listed) if t not in tokens]
    probe_task = asyncio.gather(
        *(_probe(http, company, ats, t, refresh) for t in tokens for ats in _GUESSABLE),
        *(_probe(http, company, Ats.GREENHOUSE, t, refresh) for t in listed_tokens),
    )
    if careers_url:
        probes, (refs, others, checked) = await asyncio.gather(
            probe_task, _scan_pages(http, careers_url, refresh)
        )
    else:
        probes, refs, others, checked = await probe_task, [], [], []

    found: dict[tuple[Ats, str], _Found] = {}
    for p in probes:
        if p is not None:
            p.evidence.insert(0, f"token {p.token!r} answered on {p.ats.value} ({p.jobs} jobs)")
            found[(p.ats, p.token)] = p
    for listing in listed:
        f = found.get((Ats.GREENHOUSE, listing.token.lower()))
        if f is not None:
            f.evidence.append(
                f"listed in the board directory as {listing.company!r}"
                f" (confirmed {board_dir.checked})"
            )
    for ats, token, page_url in refs:
        f = found.get((ats, token))
        if f is None:
            # A link on the page must still point at a live board (old boards get deleted).
            f = await _probe(http, company, ats, token, refresh)
            if f is None:
                continue
            f.evidence.insert(0, f"board answered ({f.jobs} jobs)")
            found[(ats, token)] = f
        if not f.on_page:
            f.on_page = True
            f.evidence.insert(0, f"careers page {page_url} links to {ats.value} board {token!r}")

    ranked = sorted(found.values(), key=lambda f: f.rank)
    if not ranked:
        evidence = [f"no board answered for tokens {tokens + listed_tokens}"]
        if others:
            evidence.append(f"careers pages reference unsupported systems: {', '.join(others)}")
        return Resolution(evidence=evidence, pages_checked=checked)
    best, rest = ranked[0], ranked[1:]
    evidence = list(best.evidence)
    if others:
        evidence.append(f"pages also reference: {', '.join(others)}")
    return Resolution(
        ats=best.ats,
        token=best.token,
        confidence=best.confidence,
        jobs=best.jobs,
        evidence=evidence,
        alternatives=[
            BoardCandidate(
                ats=f.ats, token=f.token, jobs=f.jobs, confidence=f.confidence, evidence=f.evidence
            )
            for f in rest
        ],
        pages_checked=checked,
    )
