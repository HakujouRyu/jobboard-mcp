"""Pure normalization helpers: HTML, dates, remote status, pay, employment type, fingerprints.

Rule of thumb: prefer structured fields, fall back to text, and return ``unknown``/``None``
instead of guessing. ``remote_us`` needs positive evidence that a US-based worker is eligible.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from jobboard_mcp.models import EmploymentType, Pay, PaySource, Remote

HOURS_PER_YEAR = 2080

# --------------------------------------------------------------------------- HTML


_BLOCK_END = re.compile(r"</(p|div|ul|ol|h[1-6]|tr|table|section|blockquote)\s*>", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_LI = re.compile(r"<li[^>]*>", re.I)
_DROP = re.compile(r"<(script|style)[^>]*>.*?</\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_ESCAPED_TAG = re.compile(r"&lt;/?[a-zA-Z]")


def html_to_text(value: str | None) -> str | None:
    """Unescape (Greenhouse double-escapes), strip tags, and collapse whitespace."""
    if not value:
        return None
    s = value
    if _ESCAPED_TAG.search(s):
        s = html.unescape(s)
    s = _DROP.sub("", s)
    s = _BR.sub("\n", s)
    s = _LI.sub("\n- ", s)
    s = _BLOCK_END.sub("\n", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    # str.split() is much faster than whitespace regexes on long descriptions.
    lines: list[str] = []
    for line in s.split("\n"):
        text = " ".join(line.split())
        if text or (lines and lines[-1]):
            lines.append(text)
    return "\n".join(lines).strip() or None


# --------------------------------------------------------------------------- dates


def parse_date(value: str | int | float | None) -> date | None:
    """ISO-8601 strings or epoch timestamps (ms or s) to a UTC calendar date."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, int | float):
            seconds = value / 1000 if value > 10**11 else value
            return datetime.fromtimestamp(seconds, tz=UTC).date()
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.date()


# --------------------------------------------------------------------------- remote

_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "district of columbia",
}  # fmt: skip
# "CA" is left out on purpose: it is also Canada's country code.
_US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM",
    "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA",
    "WA", "WV", "WI", "WY", "DC",
}  # fmt: skip
_US_PATTERNS = re.compile(
    # AMER / NAMER / NORAM are region shorthands that include the US.
    r"\b(united states|usa|us|north america|americas|amer|namer|noram|us[- ]based)\b"
    r"|\bu\.s\.(a\.)?(?!\w)"
    r"|\b(" + "|".join(sorted(_US_STATES, key=len, reverse=True)) + r")\b",
    re.I,
)
_US_ABBR = re.compile(r",\s*(" + "|".join(sorted(_US_STATE_ABBR)) + r")\b")
_GLOBAL = re.compile(r"\b(anywhere|worldwide|world[- ]wide|global(ly)?|any location)\b", re.I)
# Places that are unambiguously outside the US. Not exhaustive: unrecognized places simply
# contribute no region, which keeps them out of remote_us.
_NON_US = re.compile(
    r"\b(canada|mexico|brazil|argentina|colombia|chile|peru|latam|latin america|"
    r"europe|european union|emea|eu|uk|united kingdom|england|scotland|wales|ireland|"
    r"germany|france|spain|portugal|italy|netherlands|belgium|poland|sweden|norway|denmark|"
    r"finland|switzerland|austria|czech(ia| republic)?|romania|greece|israel|turkey|"
    r"india|bangalore|bengaluru|hyderabad|pune|apac|asia|singapore|japan|tokyo|korea|china|"
    r"hong kong|taiwan|philippines|vietnam|thailand|indonesia|malaysia|australia|"
    r"new zealand|south africa|nigeria|kenya|egypt|uae|dubai|saudi arabia|"
    r"london|berlin|paris|amsterdam|dublin|toronto|vancouver|montreal|sydney|melbourne)\b",
    re.I,
)
# Words that name only a country, region, state, or time zone. A location made only of these
# ("United States", "ANZ Region", "United States - West Coast", "Ontario, Canada") says where
# someone may live, not where an office is, so it is not evidence of on-site work.
_COUNTRIES = {
    "united states", "united states of america", "usa", "us", "u.s.", "canada", "mexico",
    "brazil", "argentina", "colombia", "chile", "peru", "costa rica", "united kingdom", "uk",
    "great britain", "england", "scotland", "wales", "northern ireland", "ireland", "germany",
    "france", "spain", "portugal", "italy", "netherlands", "belgium", "luxembourg", "poland",
    "sweden", "norway", "denmark", "finland", "iceland", "switzerland", "austria", "czechia",
    "czech republic", "slovakia", "hungary", "romania", "bulgaria", "greece", "croatia",
    "serbia", "slovenia", "estonia", "latvia", "lithuania", "ukraine", "israel", "turkey",
    "india", "pakistan", "bangladesh", "sri lanka", "singapore", "japan", "south korea", "korea",
    "china", "hong kong", "taiwan", "philippines", "vietnam", "thailand", "indonesia",
    "malaysia", "australia", "new zealand", "south africa", "nigeria", "kenya", "egypt",
    "uae", "united arab emirates", "saudi arabia", "ksa", "qatar",
}  # fmt: skip
_REGION_TERMS = {
    "north america", "south america", "latin america", "central america", "americas", "amer",
    "namer", "noram", "latam", "emea", "apac", "anz", "apj", "europe", "european union", "eu",
    "eea", "asia", "asia pacific", "middle east", "africa", "oceania", "nordics", "dach",
    "benelux", "region", "regions", "west", "east", "north", "south", "central", "western",
    "eastern", "northern", "southern", "coast", "time", "zone", "zones", "timezone", "pacific",
    "mountain", "atlantic", "est", "pst", "cst", "mst", "et", "pt", "ct", "cet", "gmt", "utc",
    "or", "and", "only", "based", "the", "of", "in", "any", "state", "states", "province",
    "ontario", "quebec", "british columbia", "alberta", "manitoba", "nova scotia",
}  # fmt: skip
_PLACE_PHRASES = sorted(
    _COUNTRIES | _REGION_TERMS | _US_STATES | {"district of columbia"}, key=len, reverse=True
)
_PLACE_PHRASE = re.compile(r"\b(" + "|".join(re.escape(p) for p in _PLACE_PHRASES) + r")\b", re.I)
# Upper-case state and province codes only, so ordinary words are not mistaken for codes.
_STATE_CODE = re.compile(
    r"\b(" + "|".join(sorted(_US_STATE_ABBR | {"CA", "BC", "ON", "QC"})) + r")\b"
)
_REMOTE_WORD = re.compile(r"\b(remote|anywhere|work from home|wfh|distributed)\b", re.I)
_HYBRID_WORD = re.compile(r"\bhybrid\b", re.I)
_US_COUNTRY = {"us", "usa", "united states", "united states of america"}


def _segments(locations: Iterable[str]) -> list[str]:
    out: list[str] = []
    for loc in locations:
        out.extend(p.strip() for p in re.split(r"[;|]", loc) if p.strip())
    return out


def _region(segment: str) -> str | None:
    """'us', 'global', 'other', or None when the segment names nothing recognizable.

    Spelled-out US markers win ("Remote - US or Canada" is open to US workers). State
    abbreviations are checked only after non-US places, so "Bangalore, IN" stays non-US.
    """
    if _US_PATTERNS.search(segment):
        return "us"
    if _NON_US.search(segment):
        return "other"
    if _US_ABBR.search(segment):
        return "us"
    if _GLOBAL.search(segment):
        return "global"
    return None


def _country_region(country: str) -> str | None:
    """Structured country values can still be lists ("US | EU"); any US part counts."""
    regions: set[str] = set()
    for part in re.split(r"[|;,/]", country):
        c = part.strip().lower()
        if c:
            regions.add("us" if c in _US_COUNTRY else _region(part) or "other")
    if "us" in regions:
        return "us"
    return "other" if regions else None


def _region_only(segment: str) -> bool:
    """True when a location names no city: only countries, regions, states, or time zones."""
    rest = _PLACE_PHRASE.sub(" ", segment)
    rest = _STATE_CODE.sub(" ", rest)  # "Australia - WA", "Remote, TX"
    return not re.search(r"[A-Za-z]{2,}", rest)


def classify_remote(
    locations: Sequence[str],
    workplace_type: str | None = None,
    countries: Sequence[str] = (),
) -> Remote:
    """Classify remote status.

    ``workplace_type`` is the ATS's structured field, lowercased: remote / hybrid / onsite /
    unspecified, or None when the ATS has no such field at all (Greenhouse). ``countries`` are
    structured country names or ISO codes for the job's locations.
    """
    wt = (workplace_type or "").lower().replace("-", "").replace("_", "").replace(" ", "")
    segs = _segments(locations)
    text_remote = any(_REMOTE_WORD.search(s) for s in segs)
    text_hybrid = any(_HYBRID_WORD.search(s) for s in segs)

    if wt == "hybrid":
        return Remote.HYBRID
    if wt == "onsite":
        return Remote.ONSITE
    if wt != "remote" and not text_remote:
        if text_hybrid:
            return Remote.HYBRID
        # A city with no remote/hybrid signal means on-site, but only when the ATS has no
        # structured field (an explicit "unspecified" stays unknown) and some location names
        # a city. Country- or region-only locations are common on remote-first boards.
        if workplace_type is None and segs and not all(_region_only(s) for s in segs):
            return Remote.ONSITE
        return Remote.UNKNOWN

    regions = {_region(s) for s in segs}
    regions |= {_country_region(c) for c in countries if c}
    if "us" in regions:
        return Remote.REMOTE_US
    if "global" in regions:
        return Remote.REMOTE_GLOBAL
    if "other" in regions:
        return Remote.REMOTE_OTHER
    return Remote.UNKNOWN


# --------------------------------------------------------------------------- pay

_SYMBOL_CURRENCY = {"$": "USD", "£": "GBP", "€": "EUR", "₹": "INR"}
# Letters glued to a "$": "US$", "CA$", "C$", "AU$", "A$", "S$".
_DOLLAR_PREFIX = {"US": "USD", "CA": "CAD", "C": "CAD", "AU": "AUD", "A": "AUD", "S": "SGD"}
_CODES = "USD|CAD|GBP|EUR|AUD|INR|SGD|NZD|CHF|JPY|MXN|BRL"
_SYMBOL = re.compile(r"[$£€₹]")
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_INTERVAL = (
    r"(?P<interval>\s?(?:/|per|an|a)\s?(?:hour|hr|year|yr|annum|month|mo|week|wk)\b"
    r"|\s?(?:annually|hourly|yearly)\b)?"
)
# Both patterns are matched only at currency-symbol positions (see parse_pay_text), which
# keeps scanning long descriptions cheap. A currency code before the symbol is checked
# separately by _PREFIX.
_RANGE = re.compile(
    rf"(?P<asym>[$£€₹])\s?(?P<anum>{_NUM})\s?(?P<ak>[kK]\b)?"
    rf"\s*(?:-|–|—|to)\s*"
    rf"(?:(?:{_CODES})\s?)?(?:[A-Z]{{0,2}}[$£€₹])?\s?(?P<bnum>{_NUM})\s?(?P<bk>[kK]\b)?"
    rf"(?:\s?(?P<code>{_CODES})\b)?" + _INTERVAL,
    re.I,
)
_SINGLE = re.compile(
    rf"(?P<asym>[$£€₹])\s?(?P<anum>{_NUM})\s?(?P<ak>[kK]\b)?"
    rf"(?:\s?(?P<code>{_CODES})\b)?"
    r"(?P<interval>\s?(?:/|per|an|a)\s?(?:hour|hr)\b|\s?hourly\b)",
    re.I,
)
_PREFIX = re.compile(rf"(?:\b(?P<code>{_CODES})\s?|\b(?P<glued>US|CA|C|AU|A|S))$", re.I)
_BIG_UNIT = re.compile(r"\s?(million|billion|mm|bn|m\b|b\b)", re.I)


def interval_from_label(label: str | None) -> str | None:
    """Map ATS interval labels ('1 YEAR', 'per-hour-wage', '/hr') to year/hour/month/week."""
    if not label:
        return None
    s = label.lower()
    if re.search(r"hour|\bhr\b", s):
        return "hour"
    if re.search(r"year|\byr\b|annum|annual", s):
        return "year"
    if re.search(r"month|\bmo\b", s):
        return "month"
    if re.search(r"week|\bwk\b", s):
        return "week"
    return None


def annualize(amount: float | None, interval: str | None) -> int | None:
    if amount is None:
        return None
    factor = {"year": 1, "hour": HOURS_PER_YEAR, "month": 12, "week": 52}.get(interval or "year")
    if factor is None:
        return None
    return round(amount * factor)


@dataclass(frozen=True)
class PayRange:
    min: float | None
    max: float | None
    currency: str | None
    interval: str | None  # year / hour / month / week / None (as posted, normalized)


def combine_ranges(ranges: Sequence[PayRange], raw: str | None, source: PaySource) -> Pay:
    """Overall min/max across tiers, annual USD only. Non-USD keeps currency but no numbers."""
    if not ranges:
        return Pay(raw=raw, source=PaySource.NONE if raw is None else source)
    usd = [r for r in ranges if (r.currency or "").upper() == "USD"]
    if not usd:
        first = ranges[0]
        return Pay(currency=first.currency, interval=first.interval, raw=raw, source=source)
    mins = [v for r in usd if (v := annualize(r.min, r.interval)) is not None]
    maxes = [v for r in usd if (v := annualize(r.max, r.interval)) is not None]
    intervals = {r.interval for r in usd}
    return Pay(
        min=min(mins) if mins else None,
        max=max(maxes) if maxes else None,
        currency="USD",
        interval=intervals.pop() if len(intervals) == 1 else "mixed",
        raw=raw,
        source=source,
    )


def _num(match: re.Match[str], p: str) -> float:
    value = float(match.group(f"{p}num").replace(",", ""))
    if match.group(f"{p}k"):
        value *= 1000
    return value


def _currency(match: re.Match[str], text: str) -> str:
    if match.group("code"):
        return match.group("code").upper()
    start = match.start()
    pre = _PREFIX.search(text[max(0, start - 6) : start])
    if pre and pre.group("code"):
        return pre.group("code").upper()
    if pre and pre.group("glued") and match.group("asym") == "$":
        return _DOLLAR_PREFIX[pre.group("glued").upper()]
    return _SYMBOL_CURRENCY[match.group("asym")]


def _plausible(lo: float, hi: float, interval: str | None) -> bool:
    if lo <= 0 or hi < lo:
        return False
    if interval == "hour":
        return lo >= 7 and hi <= 1000
    if interval in ("month", "week"):
        return lo >= 100 and hi <= 100_000
    # Unlabeled or annual: insist on salary-sized numbers so "$500 stipend - $2,000" is ignored.
    return lo >= 10_000 and hi <= 2_000_000


def parse_pay_text(text: str | None) -> Pay:
    """Find pay ranges ("$150,000 - $180,000", "$150K–$180K USD", "$75/hr") in free text."""
    if not text or not _SYMBOL.search(text):
        return Pay()
    flat = re.sub(r"\s+", " ", text)
    found: list[PayRange] = []
    snippets: list[str] = []
    consumed = 0
    for sym in _SYMBOL.finditer(flat):
        pos = sym.start()
        if pos < consumed:
            continue
        m = _RANGE.match(flat, pos)
        if m is not None and not _BIG_UNIT.match(flat, m.end()):
            lo, hi = _num(m, "a"), _num(m, "b")
            interval = interval_from_label(m.group("interval")) or ("hour" if hi < 1000 else None)
            if _plausible(lo, hi, interval):
                found.append(PayRange(lo, hi, _currency(m, flat), interval or "year"))
                snippets.append(m.group(0).strip())
                consumed = m.end()
                continue
        m = _SINGLE.match(flat, pos)
        if m is not None:
            v = _num(m, "a")
            if _plausible(v, v, "hour"):
                found.append(PayRange(v, v, _currency(m, flat), "hour"))
                snippets.append(m.group(0).strip())
                consumed = m.end()
    if not found:
        return Pay()
    return combine_ranges(found, " | ".join(dict.fromkeys(snippets)), PaySource.PARSED_FROM_TEXT)


# --------------------------------------------------------------------------- employment type


def employment_type(value: str | None) -> EmploymentType | None:
    """None when the ATS gives nothing; UNKNOWN when it gives something unrecognizable."""
    if value is None or not value.strip():
        return None
    s = re.sub(r"[^a-z]", "", value.lower())
    if "intern" in s:
        return EmploymentType.INTERN
    if "parttime" in s:
        return EmploymentType.PART_TIME
    if any(w in s for w in ("contract", "temporary", "temp", "freelance", "fixedterm")):
        return EmploymentType.CONTRACT
    if any(w in s for w in ("fulltime", "permanent", "regular")):
        return EmploymentType.FULL_TIME
    return EmploymentType.UNKNOWN


# --------------------------------------------------------------------------- fingerprint

_ABBREV = [
    (r"\bsr\b\.?", "senior"),
    (r"\bjr\b\.?", "junior"),
    (r"\beng\b\.?", "engineer"),
    (r"\bmgr\b\.?", "manager"),
    (r"&", " and "),
]


def normalize_text(value: str) -> str:
    s = value.lower()
    for pattern, repl in _ABBREV:
        s = re.sub(pattern, repl, s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def fingerprint(company: str, title: str, locations: Iterable[str]) -> str:
    locs = sorted({normalize_text(seg) for seg in _segments(locations)})
    key = "|".join([normalize_text(company), normalize_text(title), ";".join(locs)])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
