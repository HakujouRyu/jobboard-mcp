from datetime import date

import pytest

from jobboard_mcp.models import EmploymentType, PaySource, Remote
from jobboard_mcp.normalize import (
    PayRange,
    annualize,
    classify_remote,
    combine_ranges,
    employment_type,
    fingerprint,
    html_to_text,
    https_url,
    interval_from_label,
    parse_date,
    parse_pay_text,
)

# ------------------------------------------------------------------ remote: text only (Greenhouse)


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        # Real gitlab strings
        ("Remote, Canada; Remote, United States", Remote.REMOTE_US),
        ("Remote, United States", Remote.REMOTE_US),
        ("Remote, US", Remote.REMOTE_US),
        (
            "Remote, Canada; Remote, Israel; Remote, United Kingdom; Remote, United States",
            Remote.REMOTE_US,
        ),
        ("Remote, United Kingdom", Remote.REMOTE_OTHER),
        ("Remote, Canada; Remote, United Kingdom", Remote.REMOTE_OTHER),
        ("Remote Ireland; Remote, United Kingdom", Remote.REMOTE_OTHER),
        ("Remote, France", Remote.REMOTE_OTHER),
        ("Bangalore, India", Remote.ONSITE),
        # Bare "Remote" names no region: never guess remote_us
        ("Remote", Remote.UNKNOWN),
        ("Remote - Austin", Remote.UNKNOWN),
        # Other common shapes
        ("Remote - US or Canada", Remote.REMOTE_US),
        ("Remote (U.S.)", Remote.REMOTE_US),
        ("Remote - North America", Remote.REMOTE_US),
        ("Remote, Denver, CO", Remote.REMOTE_US),
        ("Remote - Bangalore, IN", Remote.REMOTE_OTHER),
        ("Remote - EMEA", Remote.REMOTE_OTHER),
        # Supabase
        ("Remote, AMER", Remote.REMOTE_US),
        ("Remote, APAC", Remote.REMOTE_OTHER),
        ("Remote, Global", Remote.REMOTE_GLOBAL),
        ("Anywhere", Remote.REMOTE_GLOBAL),
        ("Remote - Worldwide", Remote.REMOTE_GLOBAL),
        ("Hybrid - New York", Remote.HYBRID),
        ("San Francisco, CA", Remote.ONSITE),
        # Country/region-only locations are not on-site evidence (Mattermost, Buildkite).
        # US ones stay unknown; non-US ones can't be remote_us, so they're remote_other.
        ("United States", Remote.UNKNOWN),
        ("United States - West Coast", Remote.UNKNOWN),
        ("Texas", Remote.UNKNOWN),
        ("Pacific Time", Remote.UNKNOWN),
        ("AMER", Remote.UNKNOWN),
        ("ANZ Region", Remote.REMOTE_OTHER),
        ("United Kingdom, Ireland or Poland", Remote.REMOTE_OTHER),
        ("Australia - WA", Remote.REMOTE_OTHER),
        ("Ontario, Canada", Remote.REMOTE_OTHER),
        ("EMEA", Remote.REMOTE_OTHER),
        ("APJ", Remote.REMOTE_OTHER),
        ("The Netherlands; Portugal; United Kingdom; Spain", Remote.REMOTE_OTHER),
        ("Singapore", Remote.REMOTE_OTHER),
        ("UAE", Remote.REMOTE_OTHER),
        ("United States; Portugal", Remote.UNKNOWN),
        # A city anywhere in the list is on-site evidence
        ("Ontario, Canada; Toronto, Ontario, Canada", Remote.ONSITE),
        ("Tokyo, Japan", Remote.ONSITE),
        ("Houston, TX", Remote.ONSITE),
    ],
)
def test_classify_remote_text_only(location: str, expected: Remote) -> None:
    assert classify_remote([location]) is expected


def test_classify_remote_no_locations_is_unknown() -> None:
    assert classify_remote([]) is Remote.UNKNOWN


@pytest.mark.parametrize(
    ("locations", "title", "expected"),
    [
        # ClickHouse (Ashby, no workplaceType): the title carries the remote signal
        (["AMER", "United States"], "Senior Software Engineer - Cloud Infrastructure - (US Remote)",
         Remote.REMOTE_US),
        (["EMEA"], "QA Engineer - Core Database (remote)", Remote.REMOTE_OTHER),
        (["Singapore", "Australia"], "Senior Site Reliability Engineer- (APJ-Remote)",
         Remote.REMOTE_OTHER),
        # Brackets without a remote word, or "remote" outside brackets, are ignored
        (["AMER"], "Senior Software Engineer (Backend)", Remote.UNKNOWN),
        (["San Francisco, CA"], "Remote Sensing Engineer", Remote.ONSITE),
    ],
)  # fmt: skip
def test_classify_remote_title_hint(locations: list[str], title: str, expected: Remote) -> None:
    assert classify_remote(locations, None, title=title) is expected


def test_title_hint_does_not_beat_structured_workplace() -> None:
    assert classify_remote(["Austin, TX"], "Hybrid", title="Engineer (Remote)") is Remote.HYBRID


# ------------------------------------------------------------------ remote: structured fields


def test_structured_hybrid_beats_remote_text() -> None:
    # Temporal: isRemote=true but workplaceType Hybrid
    assert classify_remote(["San Francisco, California"], "Hybrid", ["United States"]) is (
        Remote.HYBRID
    )


def test_structured_remote_uses_country() -> None:
    assert classify_remote(["Seattle, Washington"], "remote", ["United States"]) is (
        Remote.REMOTE_US
    )
    # Lever: remote in Hyderabad, country IN
    assert classify_remote(["Hyderabad"], "remote", ["IN"]) is Remote.REMOTE_OTHER


def test_structured_country_lists() -> None:
    # PostHog: addressCountry "US | EU"
    assert classify_remote(["Remote"], "Remote", ["US | EU"]) is Remote.REMOTE_US
    assert classify_remote(["Remote"], "Remote", ["European Union"]) is Remote.REMOTE_OTHER


def test_structured_remote_region_text() -> None:
    assert classify_remote(["North America"], "Remote") is Remote.REMOTE_US
    assert classify_remote(["Europe"], "Remote", ["European Union"]) is Remote.REMOTE_OTHER


def test_structured_remote_without_region_is_unknown() -> None:
    assert classify_remote(["Remote"], "remote") is Remote.UNKNOWN


def test_any_us_location_wins() -> None:
    assert classify_remote(["North America", "Europe"], "Remote") is Remote.REMOTE_US


def test_unspecified_is_unknown_not_onsite() -> None:
    # leverdemo: workplaceType "unspecified"
    assert classify_remote(["Baltimore, MD"], "unspecified", ["US"]) is Remote.UNKNOWN


def test_structured_onsite() -> None:
    assert classify_remote(["Houston, TX"], "onsite", ["US"]) is Remote.ONSITE


# ------------------------------------------------------------------ pay: text


@pytest.mark.parametrize(
    ("text", "lo", "hi", "interval"),
    [
        (
            "United States Salary Range $139,200 \u2013 $235,200 USD How GitLab",
            139_200,
            235_200,
            "year",
        ),
        ("$150,000 - $180,000", 150_000, 180_000, "year"),
        ("The range is $150K\u2013$180K plus equity", 150_000, 180_000, "year"),
        ("$172.8K \u2013 $283.5K", 172_800, 283_500, "year"),
        ("USD 120,000 to 140,000 per year", None, None, None),  # no $ symbol: not parsed
        ("$75/hr", 156_000, 156_000, "hour"),
        ("$60 - $80 per hour", 124_800, 166_400, "hour"),
        ("$45 an hour", 93_600, 93_600, "hour"),
    ],
)
def test_parse_pay_text(text: str, lo: int | None, hi: int | None, interval: str | None) -> None:
    pay = parse_pay_text(text)
    assert (pay.min, pay.max, pay.interval) == (lo, hi, interval)
    if lo is not None:
        assert pay.source is PaySource.PARSED_FROM_TEXT
        assert pay.currency == "USD"


def test_parse_pay_text_multiple_tiers_report_overall_range() -> None:
    text = "Zone A: $180,000 - $220,000. Zone B: $160,000 - $200,000."
    pay = parse_pay_text(text)
    assert (pay.min, pay.max) == (160_000, 220_000)
    assert pay.raw is not None and "Zone" not in pay.raw and " | " in pay.raw


def test_parse_pay_text_mixed_currency_keeps_only_usd() -> None:
    text = "US: $150,000 - $180,000 USD. Canada: $140,000 - $170,000 CAD."
    pay = parse_pay_text(text)
    assert (pay.min, pay.max, pay.currency) == (150_000, 180_000, "USD")
    assert pay.raw is not None and "CAD" in pay.raw


@pytest.mark.parametrize(
    ("text", "currency"),
    [
        ("Base pay: C$120,000 - C$150,000", "CAD"),
        ("CA$120,000 - 150,000", "CAD"),
        ("CAD $120,000 - $150,000", "CAD"),
        ("USD $120,000 - $150,000", "USD"),
        ("US$120,000 - US$150,000", "USD"),
        ("AU$120,000 - AU$150,000", "AUD"),
        ("€80,000 \u2013 €95,000", "EUR"),
        ("pays a $120,000 - $150,000 salary", "USD"),  # the word "a" is not a prefix
    ],
)
def test_parse_pay_text_currency_prefixes(text: str, currency: str) -> None:
    assert parse_pay_text(text).currency == currency


@pytest.mark.parametrize(
    ("text", "currency", "interval"),
    [
        # Mozilla: European thousands separators are not decimals, so this isn't hourly
        ("€68.000\u2013€91.000 EUR", "EUR", "year"),
        ("Gehalt: 58.000 € bis €77.000,00", "EUR", None),  # leading figure has no symbol
        ("€55.000,50 - €70.000,00", "EUR", "year"),
    ],
)
def test_parse_pay_text_dot_thousands(text: str, currency: str, interval: str | None) -> None:
    pay = parse_pay_text(text)
    assert pay.interval == interval
    if interval is not None:
        assert pay.currency == currency


def test_parse_pay_text_cents_are_not_thousands() -> None:
    # Wikimedia: an hourly range written with cents
    pay = parse_pay_text("$56.07 to US$87.13")
    assert (pay.min, pay.max, pay.interval) == (116_626, 181_230, "hour")


def test_parse_pay_text_non_usd_only() -> None:
    pay = parse_pay_text("Salary £70,000 - £90,000")
    assert (pay.min, pay.max, pay.currency) == (None, None, "GBP")
    assert pay.source is PaySource.PARSED_FROM_TEXT


@pytest.mark.parametrize(
    "text",
    [
        "We offer a $500 - $2,000 learning stipend",  # not salary-sized
        "Raised $100 - $200 million in funding",
        "A $1,000 home office budget",
        "",
        None,
    ],
)
def test_parse_pay_text_ignores_non_salary(text: str | None) -> None:
    pay = parse_pay_text(text)
    assert pay.min is None and pay.source is PaySource.NONE


# ------------------------------------------------------------------ pay: structured helpers


def test_combine_ranges_usd_and_hourly() -> None:
    pay = combine_ranges([PayRange(40, 50, "USD", "hour")], raw="r", source=PaySource.STRUCTURED)
    assert (pay.min, pay.max, pay.interval) == (83_200, 104_000, "hour")


def test_combine_ranges_non_usd() -> None:
    pay = combine_ranges([PayRange(1, 2, "GBP", "year")], raw="r", source=PaySource.STRUCTURED)
    assert (pay.min, pay.max, pay.currency) == (None, None, "GBP")


@pytest.mark.parametrize(
    ("label", "expected"),
    [("1 YEAR", "year"), ("per-year-salary", "year"), ("per-hour-wage", "hour"),
     ("1 HOUR", "hour"), ("per-month-salary", "month"), ("/hr", "hour"), (None, None),
     ("one-time", None)],
)  # fmt: skip
def test_interval_from_label(label: str | None, expected: str | None) -> None:
    assert interval_from_label(label) == expected


def test_annualize() -> None:
    assert annualize(75, "hour") == 156_000
    assert annualize(10_000, "month") == 120_000
    assert annualize(None, "year") is None


# ------------------------------------------------------------------ dates


def test_parse_date_variants() -> None:
    assert parse_date("2026-10-02T11:31:50-04:00") == date(2026, 10, 2)
    assert parse_date("2021-04-27T20:13:45.158+00:00") == date(2021, 4, 27)
    assert parse_date("2026-10-02T22:00:00-04:00") == date(2026, 10, 3)  # UTC rollover
    assert parse_date("2026-01-05T00:00:00Z") == date(2026, 1, 5)
    assert parse_date(1791566108174) == date(2026, 10, 9)  # Lever epoch ms
    assert parse_date(None) is None
    assert parse_date("not a date") is None


# ------------------------------------------------------------------ employment type


@pytest.mark.parametrize(
    ("value", "expected"),
    [("FullTime", EmploymentType.FULL_TIME), ("Full-Time", EmploymentType.FULL_TIME),
     ("Permanent", EmploymentType.FULL_TIME),
     ("Regular Full Time (Salary)", EmploymentType.FULL_TIME),
     ("PartTime", EmploymentType.PART_TIME), ("Contract", EmploymentType.CONTRACT),
     ("Temporary", EmploymentType.CONTRACT), ("Intern", EmploymentType.INTERN),
     ("Remote", EmploymentType.UNKNOWN), (None, None), ("", None)],
)  # fmt: skip
def test_employment_type(value: str | None, expected: EmploymentType | None) -> None:
    assert employment_type(value) == expected


@pytest.mark.parametrize(
    ("value", "title", "expected"),
    [
        # Wikimedia and Mozilla titles
        (None, "Senior Software Engineer, Android (Contract)", EmploymentType.CONTRACT),
        (None, "Contract Staff Software Engineer-FireFox Layout", EmploymentType.CONTRACT),
        ("Full-time", "Communications Lead, Wikimedia Enterprise (Contract)",
         EmploymentType.CONTRACT),
        (None, "Backend Engineer [Contract, 6 months]", EmploymentType.CONTRACT),
        (None, "Data Engineer - Contractor", EmploymentType.CONTRACT),
        (None, "Freelance Technical Writer", EmploymentType.CONTRACT),
        ("Remote", "QA Engineer (Fixed-Term)", EmploymentType.CONTRACT),
        # Roles that handle contracts are not contract roles
        (None, "Contract Manager", None),
        (None, "Contracts Specialist", None),
        ("Full-time", "Senior Contract Negotiator", EmploymentType.FULL_TIME),
        # Intern and part-time are more specific and are kept
        ("Intern", "Software Engineering Intern (Temporary)", EmploymentType.INTERN),
        ("Full-time", "Senior Software Engineer", EmploymentType.FULL_TIME),
    ],
)  # fmt: skip
def test_employment_type_from_title(
    value: str | None, title: str, expected: EmploymentType | None
) -> None:
    assert employment_type(value, title) == expected


# ------------------------------------------------------------------ html


def test_html_to_text_greenhouse_double_escaped() -> None:
    raw = (
        "&lt;div&gt;&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;"
        "&lt;ul&gt;&lt;li&gt;One&lt;/li&gt;&lt;li&gt;Two&lt;/li&gt;&lt;/ul&gt;&lt;/div&gt;"
    )
    assert html_to_text(raw) == "Hello & welcome\n\n- One\n- Two"


def test_html_to_text_plain_html_and_empty() -> None:
    assert html_to_text("<p>a&nbsp;b</p><br/>c") == "a b\n\nc"
    assert html_to_text("") is None
    assert html_to_text(None) is None


# ------------------------------------------------------------------ fingerprint


def test_fingerprint_normalizes_title_and_location_order() -> None:
    a = fingerprint("GitLab", "Sr. Backend Engineer", ["Remote, US; Remote, Canada"])
    b = fingerprint("gitlab", "Senior Backend Engineer", ["Remote, Canada", "Remote, US"])
    assert a == b
    assert a != fingerprint("GitLab", "Staff Backend Engineer", ["Remote, US"])


# ------------------------------------------------------------------ urls


def test_https_url() -> None:
    # CircleCI's Greenhouse board links to http://www.circleci.com/careers/jobs/...
    assert https_url("http://www.circleci.com/careers/jobs/1/?gh_jid=1") == (
        "https://www.circleci.com/careers/jobs/1/?gh_jid=1"
    )
    assert https_url("HTTP://example.com/x") == "https://example.com/x"
    assert https_url("https://jobs.lever.co/a/b") == "https://jobs.lever.co/a/b"
