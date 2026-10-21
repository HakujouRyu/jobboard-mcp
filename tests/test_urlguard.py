"""The careers-page guard: public addresses only, checked on every redirect, pinned on connect."""

from collections.abc import Callable

import httpx
import pytest
import respx

from jobboard_mcp.config import Settings
from jobboard_mcp.http import Blocked, HttpClient
from jobboard_mcp.models import Ats
from jobboard_mcp.resolve import resolve_board
from jobboard_mcp.urlguard import (
    BlockedURL,
    GuardedTransport,
    Resolver,
    check_shape,
    connect_order,
    is_public,
)

PUBLIC = "93.184.215.14"


def resolver(table: dict[str, list[str]]) -> Resolver:
    async def resolve(host: str, _port: int) -> list[str]:
        return table[host]

    return resolve


def guarded(
    handler: Callable[[httpx.Request], httpx.Response],
    table: dict[str, list[str]],
    *,
    pin: bool = False,
) -> GuardedTransport:
    transport = GuardedTransport(httpx.MockTransport(handler), resolver=resolver(table))
    transport.pin = pin
    return transport


def ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="<html></html>")


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:169.254.169.254",
    ],
)
def test_non_public_addresses(address: str) -> None:
    assert not is_public(address)


@pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses(address: str) -> None:
    assert is_public(address)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://example.com/careers", "only http and https"),
        ("file:///etc/passwd", "only http and https"),
        ("https://user:pass@example.com/", "username or password"),
        ("http://example.com:8080/", "default ports"),
        ("https://example.com:80/", "default ports"),
    ],
)
def test_shape_is_checked(url: str, message: str) -> None:
    with pytest.raises(BlockedURL, match=message):
        check_shape(httpx.URL(url))


def test_explicit_default_port_is_fine() -> None:
    check_shape(httpx.URL("https://example.com:443/careers"))


async def test_host_resolving_to_private_address_is_blocked() -> None:
    transport = guarded(ok, {"intranet.example.com": ["10.0.0.5"]})
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(BlockedURL, match=r"non-public address \(10\.0\.0\.5\)"):
            await client.get("https://intranet.example.com/")


async def test_any_private_address_blocks_the_host() -> None:
    # The system resolver may connect to any of them, so one bad answer is enough.
    transport = guarded(ok, {"mixed.example.com": [PUBLIC, "127.0.0.1"]})
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(BlockedURL):
            await client.get("https://mixed.example.com/")


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/"])
async def test_ip_literals_are_checked_without_dns(url: str) -> None:
    transport = guarded(ok, {})
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(BlockedURL, match="is not a public address"):
            await client.get(url)


async def test_redirect_to_internal_address_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "evil.example.com":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})
        return httpx.Response(200, text="metadata")

    transport = guarded(handler, {"evil.example.com": [PUBLIC]})
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(BlockedURL):
            await client.get("https://evil.example.com/careers")


async def test_redirect_to_a_host_that_resolves_privately_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://internal.example.com/"})

    table = {"evil.example.com": [PUBLIC], "internal.example.com": ["192.168.0.10"]}
    transport = guarded(handler, table)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(BlockedURL, match=r"internal.example.com"):
            await client.get("https://evil.example.com/")


async def test_pinning_connects_to_the_checked_address() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    transport = guarded(handler, {"jobs.example.com": [PUBLIC]}, pin=True)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://jobs.example.com/careers?x=1")
    (sent,) = seen
    assert str(sent.url) == f"https://{PUBLIC}/careers?x=1"
    assert sent.headers["Host"] == "jobs.example.com"
    assert sent.extensions["sni_hostname"] == "jobs.example.com"
    # Callers still see the URL they asked for.
    assert str(response.url) == "https://jobs.example.com/careers?x=1"


V6 = "2606:4700:4700::1111"


@pytest.mark.parametrize(
    ("addresses", "order"),
    [
        ([PUBLIC], [PUBLIC]),
        ([V6, PUBLIC, "8.8.8.8"], [V6, PUBLIC]),
        ([PUBLIC, "8.8.8.8", V6], [PUBLIC, V6]),
        ([PUBLIC, "8.8.8.8"], [PUBLIC]),
    ],
)
def test_connect_order_is_one_address_per_family(addresses: list[str], order: list[str]) -> None:
    assert connect_order(addresses) == order


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_unreachable_family_falls_back_to_the_other(
    error: type[httpx.TransportError],
) -> None:
    # A runner without an IPv6 route, for a host whose resolver lists IPv6 first.
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        if request.url.host == V6:
            raise error("no route to host", request=request)
        return httpx.Response(200, text="ok")

    transport = guarded(handler, {"jobs.example.com": [V6, PUBLIC]}, pin=True)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://jobs.example.com/careers")
    assert response.text == "ok"
    assert tried == [V6, PUBLIC]


async def test_last_connect_error_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused by {request.url.host}", request=request)

    transport = guarded(handler, {"jobs.example.com": [V6, PUBLIC]}, pin=True)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ConnectError, match=f"refused by {PUBLIC}"):
            await client.get("https://jobs.example.com/careers")


async def test_dns_failure_is_a_connect_error() -> None:
    async def fail(_host: str, _port: int) -> list[str]:
        raise OSError("Name or service not known")

    transport = GuardedTransport(httpx.MockTransport(ok), resolver=fail)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ConnectError, match=r"DNS lookup for nowhere.test failed"):
            await client.get("https://nowhere.test/")


async def test_get_text_raises_blocked_and_never_retries() -> None:
    calls: list[str] = []

    async def resolve(host: str, _port: int) -> list[str]:
        calls.append(host)
        return ["10.0.0.1"]

    client = HttpClient(Settings(), resolver=resolve)
    try:
        with pytest.raises(Blocked, match=r"blocked: internal.test resolves to a non-public"):
            await client.get_text("https://internal.test/")
    finally:
        await client.aclose()
    assert calls == ["internal.test"]


@respx.mock
async def test_resolve_board_reports_blocked_page_and_still_probes() -> None:
    respx.get(url__regex=r"https://api\.ashbyhq\.com/.*/acme\b.*").mock(
        return_value=httpx.Response(200, json={"jobs": [{}]})
    )
    respx.get(
        url__regex=r"https://(boards-api\.greenhouse\.io|api\.ashbyhq\.com|api\.lever\.co|www\.workable\.com|api\.rippling\.com|[a-z0-9-]+\.jobs\.personio\.de)/.*"
    ).mock(return_value=httpx.Response(404))
    client = HttpClient(Settings())
    try:
        r = await resolve_board(client, "Acme", "http://localhost:8080/careers")
    finally:
        await client.aclose()
    assert (r.ats, r.token) == (Ats.ASHBY, "acme")
    assert r.pages_checked == [
        "http://localhost:8080/careers (blocked: only default ports are fetched, not 8080)"
    ]
