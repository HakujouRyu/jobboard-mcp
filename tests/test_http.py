import asyncio

import httpx
import pytest
import respx

from jobboard_mcp.config import Settings
from jobboard_mcp.http import FetchError, FetchTimeout, HttpClient, NotFound

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
