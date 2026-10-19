"""Keep careers-page fetches on the public internet (SSRF protection).

Careers URLs come from the MCP client, so a server other people can reach could otherwise be
pointed at internal addresses. ``GuardedTransport`` checks every request, redirects included:

- http or https on the default port, with no credentials in the URL;
- the host must resolve only to public addresses;
- the connection goes to the address that was checked ("pinning"), so a DNS answer that
  changes between the check and the connect (DNS rebinding) can't slip through. TLS still
  verifies the certificate against the real hostname, via httpcore's ``sni_hostname``.

Job board API requests don't go through here: their hosts are fixed in the adapters.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable

import httpx

Resolver = Callable[[str, int], Awaitable[list[str]]]

_DEFAULT_PORTS = {"http": 80, "https": 443}


class BlockedURL(httpx.RequestError):
    """The URL points somewhere the server won't go. HttpClient raises it as ``Blocked``."""


async def resolve_host(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    # IPv6 link-local results can carry a scope ("fe80::1%eth0"); the address is what we check.
    return sorted({str(info[4][0]).split("%")[0] for info in infos})


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_shape(url: httpx.URL) -> None:
    """Raise BlockedURL unless the URL is plain http(s) on the default port."""
    if url.scheme not in _DEFAULT_PORTS:
        raise BlockedURL(f"only http and https URLs are fetched, not {url.scheme!r}")
    if url.userinfo:
        raise BlockedURL("URLs with a username or password are not fetched")
    if url.port not in (None, _DEFAULT_PORTS[url.scheme]):
        raise BlockedURL(f"only default ports are fetched, not {url.port}")
    if not url.host:
        raise BlockedURL("the URL has no host")


class GuardedTransport(httpx.AsyncBaseTransport):
    """Wraps a transport so every request (and redirect hop) goes only to public addresses."""

    # Tests turn pinning off so respx routes still match on hostnames; see tests/conftest.py.
    pin = True

    def __init__(
        self, inner: httpx.AsyncBaseTransport, *, resolver: Resolver | None = None
    ) -> None:
        self._inner = inner
        self._resolver = resolver

    async def _addresses(self, request: httpx.Request) -> list[str]:
        url = request.url
        host = url.host
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return [host]
        resolver = self._resolver or resolve_host
        try:
            addresses = await resolver(host, url.port or _DEFAULT_PORTS[url.scheme])
        except OSError as exc:
            raise httpx.ConnectError(
                f"DNS lookup for {host} failed: {exc}", request=request
            ) from exc
        if not addresses:
            raise httpx.ConnectError(f"DNS lookup for {host} found no addresses", request=request)
        return addresses

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        check_shape(url)
        addresses = await self._addresses(request)
        # Refuse if any address is private: the system resolver may pick any of them.
        private = [a for a in addresses if not is_public(a)]
        literal = addresses == [url.host]
        if private and literal:
            raise BlockedURL(f"{url.host} is not a public address")
        if private:
            raise BlockedURL(f"{url.host} resolves to a non-public address ({private[0]})")
        if not self.pin or literal:
            return await self._inner.handle_async_request(request)
        pinned = httpx.Request(
            request.method,
            url.copy_with(host=addresses[0]),
            headers=request.headers,  # keeps the original Host header
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": url.host},
        )
        return await self._inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self._inner.aclose()
