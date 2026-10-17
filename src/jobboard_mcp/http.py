"""Shared async HTTP client: cache, conditional requests, retries, and concurrency caps.

Careers pages (``get_text``) are fetched through ``urlguard.GuardedTransport``, which keeps them
on the public internet. JSON requests only ever go to the job board APIs.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobboard_mcp.config import Settings
from jobboard_mcp.urlguard import BlockedURL, GuardedTransport, Resolver

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_BACKOFF_SECONDS = 10.0


class FetchError(Exception):
    """Base class; ``kind`` maps onto a per-company status."""

    kind = "error"

    def __init__(self, url: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.url, self.status = url, status


class NotFound(FetchError):
    kind = "not_found"


class FetchTimeout(FetchError):
    kind = "timeout"


class Blocked(FetchError):
    """A careers-page URL that isn't a public http(s) address. Never retried."""

    kind = "blocked"


@dataclass
class _Entry:
    data: Any
    etag: str | None
    last_modified: str | None
    stored_at: float


@dataclass(frozen=True)
class TextPage:
    url: str  # final URL after redirects
    status: int
    text: str


@dataclass(frozen=True)
class Fetched:
    data: Any
    cache_hit: bool


def _retry_after(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("Retry-After")
    if value:
        seconds: float | None
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
            except (TypeError, ValueError):  # neither seconds nor a date: back off as usual
                seconds = None
        if seconds is not None and math.isfinite(seconds):
            return min(max(seconds, 0.0), MAX_BACKOFF_SECONDS)
    return min(0.5 * 2**attempt, MAX_BACKOFF_SECONDS)


class HttpClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        resolver: Resolver | None = None,
    ) -> None:
        self._settings = settings
        headers = {"User-Agent": settings.user_agent, "Accept": "application/json"}
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )
        # Pinned connections are keyed by IP, so one kept alive for one site could be reused
        # for another site on the same IP with the wrong TLS name. Hence no keep-alive. Proxy
        # settings from the environment are ignored because a proxy would bypass the guard.
        self._pages = httpx.AsyncClient(
            headers=headers,
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            transport=GuardedTransport(
                transport
                or httpx.AsyncHTTPTransport(limits=httpx.Limits(max_keepalive_connections=0)),
                resolver=resolver,
            ),
            trust_env=False,
        )
        self._clock, self._sleep = clock, sleep
        self._cache: dict[str, _Entry] = {}
        self._inflight: dict[str, asyncio.Task[Fetched]] = {}
        self._global = asyncio.Semaphore(settings.max_concurrency)
        self._hosts: dict[str, asyncio.Semaphore] = {}

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._pages.aclose()

    def clear_cache(self) -> None:
        self._cache.clear()

    async def get_json(self, url: str, *, refresh: bool = False) -> Fetched:
        """GET a JSON document. Fresh cache entries are served without a request; stale ones
        are revalidated with If-None-Match / If-Modified-Since. ``refresh`` skips the TTL."""
        return await self._get(url, "json", refresh)

    async def get_text(self, url: str, *, refresh: bool = False) -> Fetched:
        """GET a web page. ``data`` is a TextPage; 4xx pages are returned (not raised) because
        even an error page can link to the right place. Only 2xx pages are cached. Raises
        ``Blocked`` for a URL, or a redirect, that isn't a public http(s) address."""
        return await self._get(url, "text", refresh)

    async def _get(self, url: str, kind: str, refresh: bool) -> Fetched:
        key = f"{kind} {url}"
        entry = self._cache.get(key)
        if (
            entry is not None
            and not refresh
            and self._clock() - entry.stored_at < self._settings.cache_ttl_seconds
        ):
            return Fetched(entry.data, cache_hit=True)
        # Share one request between concurrent callers of the same URL.
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fetch(url, key, kind, entry))
            self._inflight[key] = task
            task.add_done_callback(lambda _t: self._inflight.pop(key, None))
        return await asyncio.shield(task)

    def _host_sem(self, url: str) -> asyncio.Semaphore:
        host = urlsplit(url).netloc
        sem = self._hosts.get(host)
        if sem is None:
            sem = self._hosts[host] = asyncio.Semaphore(self._settings.per_host_concurrency)
        return sem

    async def _fetch(self, url: str, key: str, kind: str, entry: _Entry | None) -> Fetched:
        headers: dict[str, str] = {}
        if kind == "text":
            headers["Accept"] = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"
        if entry is not None:
            if entry.etag:
                headers["If-None-Match"] = entry.etag
            if entry.last_modified:
                headers["If-Modified-Since"] = entry.last_modified

        client = self._pages if kind == "text" else self._client
        attempt = 0
        while True:
            started = self._clock()
            try:
                # Host slot first, so waiting on a busy host doesn't hold a global slot.
                async with self._host_sem(url), self._global:
                    response = await client.get(url, headers=headers)
            except BlockedURL as exc:
                raise Blocked(url, f"blocked: {exc}") from exc
            except httpx.TimeoutException as exc:
                raise FetchTimeout(
                    url, f"timed out after {self._settings.timeout_seconds}s"
                ) from exc
            except httpx.HTTPError as exc:
                raise FetchError(url, f"{type(exc).__name__}: {exc}") from exc
            latency_ms = round((self._clock() - started) * 1000)
            status = response.status_code
            log.debug(
                "http",
                extra={"fields": {"url": url, "status": status, "latency_ms": latency_ms}},
            )

            if status in RETRY_STATUSES and attempt < self._settings.max_retries:
                await self._sleep(_retry_after(response, attempt))
                attempt += 1
                continue
            if status == 304 and entry is not None:
                entry.stored_at = self._clock()
                return Fetched(entry.data, cache_hit=True)
            if kind == "text":
                page = TextPage(url=str(response.url), status=status, text=response.text)
                if status >= 400:
                    return Fetched(page, cache_hit=False)
                data: Any = page
            else:
                if status in (404, 410):
                    raise NotFound(url, f"HTTP {status}", status)
                if status >= 400:
                    raise FetchError(url, f"HTTP {status}", status)
                try:
                    data = response.json()
                except ValueError as exc:
                    raise FetchError(url, "response was not valid JSON", status) from exc
            self._cache[key] = _Entry(
                data=data,
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
                stored_at=self._clock(),
            )
            return Fetched(data, cache_hit=False)
