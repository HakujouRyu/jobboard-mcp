import asyncio
import json

import httpx
import pytest
import respx

from jobboard_mcp.config import Settings
from jobboard_mcp.http import Blocked, FetchError, FetchTimeout, HttpClient, NotFound

URL = "https://api.example.test/board"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
async def client(clock: FakeClock, sleeps: list[float]):
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    c = HttpClient(Settings(cache_ttl_seconds=60), clock=clock, sleep=fake_sleep)
    yield c
    await c.aclose()


@respx.mock
async def test_cache_hit_within_ttl(client: HttpClient) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"n": 1}))
    first = await client.get_json(URL)
    second = await client.get_json(URL)
    assert (first.data, first.cache_hit) == ({"n": 1}, False)
    assert (second.data, second.cache_hit) == ({"n": 1}, True)
    assert route.call_count == 1


@respx.mock
async def test_sends_user_agent(client: HttpClient) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=[]))
    await client.get_json(URL)
    assert route.calls.last.request.headers["User-Agent"].startswith("jobboard-mcp/")


@respx.mock
async def test_stale_entry_revalidates_with_etag(client: HttpClient, clock: FakeClock) -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json={"n": 1}, headers={"ETag": '"v1"'}),
            httpx.Response(304),
        ]
    )
    await client.get_json(URL)
    clock.now += 61
    again = await client.get_json(URL)
    assert (again.data, again.cache_hit) == ({"n": 1}, True)
    assert route.calls.last.request.headers["If-None-Match"] == '"v1"'
    # Revalidation restarts the TTL.
    assert (await client.get_json(URL)).cache_hit
    assert route.call_count == 2


@respx.mock
async def test_refresh_bypasses_ttl(client: HttpClient) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(200, json={"n": 1}), httpx.Response(200, json={"n": 2})]
    )
    await client.get_json(URL)
    fresh = await client.get_json(URL, refresh=True)
    assert (fresh.data, fresh.cache_hit) == ({"n": 2}, False)
    assert route.call_count == 2


@respx.mock
async def test_retries_429_honoring_retry_after(client: HttpClient, sleeps: list[float]) -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert (await client.get_json(URL)).data == {"ok": True}
    assert sleeps == [3.0, 1.0]


# Garbage falls back to the usual backoff (0.5 s first); a negative wait means "now".
@pytest.mark.parametrize(("value", "expected"), [("soon", 0.5), ("nan", 0.5), ("-5", 0.0)])
@respx.mock
async def test_odd_retry_after_values(
    client: HttpClient, sleeps: list[float], value: str, expected: float
) -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": value}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert (await client.get_json(URL)).data == {"ok": True}
    assert sleeps == [expected]


@respx.mock
async def test_gives_up_after_max_retries(client: HttpClient) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(502))
    with pytest.raises(FetchError) as info:
        await client.get_json(URL)
    assert info.value.status == 502 and info.value.kind == "error"
    assert route.call_count == 3


@respx.mock
async def test_404_is_not_found(client: HttpClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(404, text="Not Found"))
    with pytest.raises(NotFound):
        await client.get_json(URL)


@respx.mock
async def test_timeout(client: HttpClient) -> None:
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FetchTimeout):
        await client.get_json(URL)


@respx.mock
async def test_invalid_json(client: HttpClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(FetchError, match="not valid JSON"):
        await client.get_json(URL)


@respx.mock
async def test_concurrent_identical_requests_share_one_fetch(client: HttpClient) -> None:
    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"n": 1})

    route = respx.get(URL).mock(side_effect=slow)
    results = await asyncio.gather(*(client.get_json(URL) for _ in range(5)))
    assert all(r.data == {"n": 1} for r in results)
    assert route.call_count == 1


@respx.mock
async def test_per_host_concurrency_cap() -> None:
    active = peak = 0

    async def track(_request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json=[])

    respx.get(url__regex=r"https://one\.test/.*").mock(side_effect=track)
    client = HttpClient(Settings(max_concurrency=8, per_host_concurrency=2))
    try:
        await asyncio.gather(*(client.get_json(f"https://one.test/{i}") for i in range(10)))
    finally:
        await client.aclose()
    assert peak == 2


@respx.mock
async def test_get_text_returns_pages_and_error_pages(client: HttpClient) -> None:
    respx.get("https://co.test/careers").mock(
        return_value=httpx.Response(200, text="<a href='/jobs'>Jobs</a>")
    )
    respx.get("https://co.test/gone").mock(return_value=httpx.Response(404, text="<p>moved</p>"))
    page = (await client.get_text("https://co.test/careers")).data
    assert (page.status, page.url) == (200, "https://co.test/careers") and "Jobs" in page.text
    assert respx.calls.last.request.headers["Accept"].startswith("text/html")
    assert (await client.get_text("https://co.test/careers")).cache_hit
    gone = await client.get_text("https://co.test/gone")
    assert gone.data.status == 404 and "moved" in gone.data.text and not gone.cache_hit


@respx.mock
async def test_text_and_json_cache_separately(client: HttpClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"a": 1}))
    assert (await client.get_json(URL)).data == {"a": 1}
    text = await client.get_text(URL)
    assert not text.cache_hit and '"a"' in text.data.text


# ------------------------------------------------------------------ robots.txt


@pytest.fixture
def robots_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(HttpClient, "check_robots", True)


def page(text: str = "<p>jobs</p>") -> httpx.Response:
    return httpx.Response(200, text=text)


@respx.mock
async def test_robots_disallowed_page_is_blocked(client: HttpClient, robots_on: None) -> None:
    robots = respx.get("https://co.test/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /internal\n")
    )
    careers = respx.get("https://co.test/careers").mock(return_value=page())
    internal = respx.get("https://co.test/internal/jobs").mock(return_value=page())
    assert (await client.get_text("https://co.test/careers")).data.status == 200
    with pytest.raises(Blocked, match=r"robots\.txt on co\.test disallows /internal/jobs"):
        await client.get_text("https://co.test/internal/jobs")
    assert (careers.call_count, internal.call_count, robots.call_count) == (1, 0, 1)
    assert robots.calls.last.request.headers["User-Agent"].startswith("jobboard-mcp/")
    # A bare text/plain gets a 406 from some sites, which would read as "no rules".
    assert robots.calls.last.request.headers["Accept"] == "text/plain,*/*;q=0.5"


@respx.mock
async def test_robots_checks_our_own_group(client: HttpClient, robots_on: None) -> None:
    respx.get("https://co.test/robots.txt").mock(
        return_value=httpx.Response(
            200, text="User-agent: *\nAllow: /\n\nUser-agent: jobboard-mcp\nDisallow: /\n"
        )
    )
    with pytest.raises(Blocked, match="disallows"):
        await client.get_text("https://co.test/careers")


@respx.mock
async def test_robots_query_counts(client: HttpClient, robots_on: None) -> None:
    respx.get("https://co.test/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /jobs?page=\n")
    )
    respx.get("https://co.test/jobs").mock(return_value=page())
    assert (await client.get_text("https://co.test/jobs")).data.status == 200
    with pytest.raises(Blocked, match=r"disallows /jobs\?page=2"):
        await client.get_text("https://co.test/jobs?page=2")


@pytest.mark.parametrize("status", [401, 403, 404, 410])
@respx.mock
async def test_robots_4xx_means_no_rules(client: HttpClient, robots_on: None, status: int) -> None:
    respx.get("https://co.test/robots.txt").mock(return_value=httpx.Response(status))
    respx.get("https://co.test/careers").mock(return_value=page())
    assert (await client.get_text("https://co.test/careers")).data.status == 200


@pytest.mark.parametrize("status", [429, 500, 503])
@respx.mock
async def test_robots_5xx_or_429_blocks(client: HttpClient, robots_on: None, status: int) -> None:
    respx.get("https://co.test/robots.txt").mock(return_value=httpx.Response(status))
    careers = respx.get("https://co.test/careers").mock(return_value=page())
    with pytest.raises(Blocked, match=rf"robots\.txt on co\.test returned HTTP {status}"):
        await client.get_text("https://co.test/careers")
    assert careers.call_count == 0


@respx.mock
async def test_robots_unreachable_fails_like_the_page(client: HttpClient, robots_on: None) -> None:
    respx.get("https://co.test/robots.txt").mock(side_effect=httpx.ConnectTimeout("slow"))
    careers = respx.get("https://co.test/careers").mock(return_value=page())
    with pytest.raises(FetchTimeout):
        await client.get_text("https://co.test/careers")
    assert careers.call_count == 0


@respx.mock
async def test_robots_checked_on_every_redirect(client: HttpClient, robots_on: None) -> None:
    respx.get("https://co.test/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://co.test/careers").mock(
        return_value=httpx.Response(302, headers={"Location": "https://ats.test/private/co"})
    )
    respx.get("https://ats.test/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    target = respx.get("https://ats.test/private/co").mock(return_value=page())
    with pytest.raises(Blocked, match=r"robots\.txt on ats\.test disallows /private/co"):
        await client.get_text("https://co.test/careers")
    assert target.call_count == 0


@respx.mock
async def test_robots_cached_per_origin_until_ttl(
    client: HttpClient, robots_on: None, clock: FakeClock
) -> None:
    robots = respx.get("https://co.test/robots.txt").mock(return_value=httpx.Response(404))
    other = respx.get("https://other.test/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(url__regex=r"https://(co|other)\.test/p\d").mock(return_value=page())
    await client.get_text("https://co.test/p1")
    await client.get_text("https://co.test/p2")
    await client.get_text("https://other.test/p1")
    assert (robots.call_count, other.call_count) == (1, 1)
    clock.now += 61  # past CACHE_TTL_SECONDS (60 in this fixture)
    await client.get_text("https://co.test/p3")
    assert robots.call_count == 2


@respx.mock
async def test_robots_fetched_once_for_concurrent_pages(
    client: HttpClient, robots_on: None
) -> None:
    robots = respx.get("https://co.test/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(url__regex=r"https://co\.test/p\d").mock(return_value=page())
    await asyncio.gather(*(client.get_text(f"https://co.test/p{i}") for i in range(5)))
    assert robots.call_count == 1


@respx.mock
async def test_robots_not_used_for_json(client: HttpClient, robots_on: None) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    await client.get_json(URL)
    assert respx.calls.call_count == 1


@respx.mock
async def test_blocked_address_reports_the_address_not_robots(
    robots_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobboard_mcp import urlguard

    async def private(_host: str, _port: int) -> list[str]:
        return ["10.0.0.5"]

    monkeypatch.setattr(urlguard, "resolve_host", private)
    c = HttpClient(Settings())
    try:
        with pytest.raises(Blocked, match="non-public address"):
            await c.get_text("https://intranet.test/careers")
    finally:
        await c.aclose()
    assert respx.calls.call_count == 0


# ------------------------------------------------------------------ pacing


@pytest.fixture
def paced(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> tuple[HttpClient, list[float]]:
    monkeypatch.setattr(HttpClient, "pace_requests", True)
    waits: list[float] = []

    async def advancing_sleep(seconds: float) -> None:
        waits.append(seconds)
        clock.now += seconds

    return HttpClient(
        Settings(per_host_interval_seconds=1.0), clock=clock, sleep=advancing_sleep
    ), waits


@respx.mock
async def test_pages_on_one_host_are_spaced(paced: tuple[HttpClient, list[float]]) -> None:
    client, waits = paced
    respx.get(url__regex=r"https://co\.test/p\d").mock(return_value=httpx.Response(200))
    respx.get(url__regex=r"https://other\.test/p\d").mock(return_value=httpx.Response(200))
    try:
        await client.get_text("https://co.test/p1")
        await client.get_text("https://other.test/p1")  # another host: no wait
        await client.get_text("https://co.test/p2")
        await client.get_text("https://co.test/p3")
    finally:
        await client.aclose()
    assert waits == [1.0, 1.0]


@respx.mock
async def test_concurrent_pages_queue_in_order(
    paced: tuple[HttpClient, list[float]], clock: FakeClock
) -> None:
    client, _ = paced
    starts: list[float] = []

    def record(_request: httpx.Request) -> httpx.Response:
        starts.append(clock.now)
        return httpx.Response(200)

    respx.get(url__regex=r"https://co\.test/p\d").mock(side_effect=record)
    try:
        await asyncio.gather(*(client.get_text(f"https://co.test/p{i}") for i in range(3)))
    finally:
        await client.aclose()
    assert starts == [1000.0, 1001.0, 1002.0]


@respx.mock
async def test_job_board_apis_are_not_paced(paced: tuple[HttpClient, list[float]]) -> None:
    client, waits = paced
    respx.get(url__startswith=URL).mock(return_value=httpx.Response(200, json={}))
    try:
        for i in range(3):
            await client.get_json(f"{URL}/{i}")
    finally:
        await client.aclose()
    assert waits == []


@respx.mock
async def test_interval_zero_turns_pacing_off(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock, sleeps: list[float]
) -> None:
    monkeypatch.setattr(HttpClient, "pace_requests", True)

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    client = HttpClient(Settings(per_host_interval_seconds=0), clock=clock, sleep=record)
    respx.get(url__regex=r"https://co\.test/p\d").mock(return_value=httpx.Response(200))
    try:
        await client.get_text("https://co.test/p1")
        await client.get_text("https://co.test/p2")
    finally:
        await client.aclose()
    assert sleeps == []


# ------------------------------------------------------------------ careers-site JSON (tier 2)

SITE = "https://acme.wd5.myworkdayjobs.test/wday/cxs/acme/External/jobs"


@respx.mock
async def test_post_site_json_sends_body_and_caches_by_body(client: HttpClient) -> None:
    route = respx.post(SITE).mock(return_value=httpx.Response(200, json={"total": 1}))
    first = await client.post_site_json(SITE, {"searchText": "engineer", "offset": 0})
    again = await client.post_site_json(SITE, {"offset": 0, "searchText": "engineer"})
    other = await client.post_site_json(SITE, {"searchText": "designer", "offset": 0})
    assert first.data == {"total": 1} and not first.cache_hit
    assert again.cache_hit and not other.cache_hit
    assert route.call_count == 2
    request = route.calls[0].request
    assert request.method == "POST"
    assert json.loads(request.content) == {"searchText": "engineer", "offset": 0}
    assert request.headers["Content-Type"] == "application/json"
    assert "If-None-Match" not in request.headers


@respx.mock
async def test_site_json_errors_match_get_json(client: HttpClient) -> None:
    respx.get(f"{SITE}/gone").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/broken").mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(NotFound):
        await client.get_site_json(f"{SITE}/gone")
    with pytest.raises(FetchError, match="not valid JSON"):
        await client.get_site_json(f"{SITE}/broken")


@respx.mock
async def test_site_json_is_robots_checked(client: HttpClient, robots_on: None) -> None:
    respx.get("https://acme.wd5.myworkdayjobs.test/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /wday/cxs/\n")
    )
    route = respx.post(SITE).mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(Blocked, match="disallows /wday/cxs/acme/External/jobs"):
        await client.post_site_json(SITE, {"offset": 0})
    assert route.call_count == 0


@respx.mock
async def test_site_json_is_paced(
    paced: tuple[HttpClient, list[float]],
) -> None:
    client, waits = paced
    respx.post(SITE).mock(return_value=httpx.Response(200, json={}))
    try:
        await client.post_site_json(SITE, {"offset": 0})
        await client.post_site_json(SITE, {"offset": 20})
    finally:
        await client.aclose()
    assert waits == [1.0]


@respx.mock
async def test_site_json_is_kept_to_public_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    from jobboard_mcp import urlguard

    async def private(_host: str, _port: int) -> list[str]:
        return ["169.254.169.254"]

    monkeypatch.setattr(urlguard, "resolve_host", private)
    c = HttpClient(Settings())
    try:
        with pytest.raises(Blocked, match="non-public address"):
            await c.post_site_json("https://metadata.test/latest", {})
    finally:
        await c.aclose()
    assert respx.calls.call_count == 0
