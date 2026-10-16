"""Shared async HTTP client: cache, conditional requests, retries, and concurrency caps."""

from __future__ import annotations

import asyncio
import email.utils
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobboard_mcp.config import Settings

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


@dataclass
class _Entry:
    data: Any
    etag: str | None
    last_modified: str | None
    stored_at: float


@dataclass(frozen=True)
class Fetched:
    data: Any
    cache_hit: bool


def _retry_after(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("Retry-After")
    if value:
        try:
            return min(float(value), MAX_BACKOFF_SECONDS)
        except ValueError:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed is not None:
                return min(max(parsed.timestamp() - time.time(), 0.0), MAX_BACKOFF_SECONDS)
    return min(0.5 * 2**attempt, MAX_BACKOFF_SECONDS)


class HttpClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )
        self._clock, self._sleep = clock, sleep
        self._cache: dict[str, _Entry] = {}
        self._inflight: dict[str, asyncio.Task[Fetched]] = {}
        self._global = asyncio.Semaphore(settings.max_concurrency)
        self._hosts: dict[str, asyncio.Semaphore] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    def clear_cache(self) -> None:
        self._cache.clear()

    async def get_json(self, url: str, *, refresh: bool = False) -> Fetched:
        """GET a JSON document. Fresh cache entries are served without a request; stale ones
        are revalidated with If-None-Match / If-Modified-Since. ``refresh`` skips the TTL."""
        entry = self._cache.get(url)
        if (
            entry is not None
            and not refresh
            and self._clock() - entry.stored_at < self._settings.cache_ttl_seconds
        ):
            return Fetched(entry.data, cache_hit=True)
        # Share one request between concurrent callers of the same URL.
        task = self._inflight.get(url)
        if task is None:
            task = asyncio.ensure_future(self._fetch(url, entry))
            self._inflight[url] = task
            task.add_done_callback(lambda _t: self._inflight.pop(url, None))
        return await asyncio.shield(task)

    def _host_sem(self, url: str) -> asyncio.Semaphore:
        host = urlsplit(url).netloc
        sem = self._hosts.get(host)
        if sem is None:
            sem = self._hosts[host] = asyncio.Semaphore(self._settings.per_host_concurrency)
        return sem

    async def _fetch(self, url: str, entry: _Entry | None) -> Fetched:
        headers: dict[str, str] = {}
        if entry is not None:
            if entry.etag:
                headers["If-None-Match"] = entry.etag
            if entry.last_modified:
                headers["If-Modified-Since"] = entry.last_modified

        attempt = 0
        while True:
            started = self._clock()
            try:
                # Host slot first, so waiting on a busy host doesn't hold a global slot.
                async with self._host_sem(url), self._global:
                    response = await self._client.get(url, headers=headers)
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
            if status in (404, 410):
                raise NotFound(url, f"HTTP {status}", status)
            if status >= 400:
                raise FetchError(url, f"HTTP {status}", status)
            try:
                data = response.json()
            except ValueError as exc:
                raise FetchError(url, "response was not valid JSON", status) from exc
            self._cache[url] = _Entry(
                data=data,
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
                stored_at=self._clock(),
            )
            return Fetched(data, cache_hit=False)
