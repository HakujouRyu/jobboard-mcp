"""Settings from environment variables, and structured JSON logging to stderr.

stdout belongs to the MCP stdio transport, so nothing here may write to it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from jobboard_mcp import __version__

REPO_URL = "https://github.com/HakujouRyu/jobboard-mcp"


def default_watchlist_path(env: Mapping[str, str], platform: str, home: Path) -> Path:
    """The per-user watchlist: under %APPDATA% on Windows, and under the XDG config folder
    (~/.config) everywhere else, macOS included."""
    if platform == "win32":
        appdata = env.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
    else:
        xdg = env.get("XDG_CONFIG_HOME", "")
        # The XDG spec says to ignore a relative path.
        base = Path(xdg) if xdg.startswith("/") else home / ".config"
    return base / "jobboard-mcp" / "watchlist.yaml"


DEFAULT_WATCHLIST = default_watchlist_path(os.environ, sys.platform, Path.home())
# Ships inside the package, and is copied to DEFAULT_WATCHLIST on first run.
EXAMPLE_WATCHLIST = Path(__file__).with_name("watchlist.example.yaml")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    watchlist_path: Path = DEFAULT_WATCHLIST
    cache_ttl_seconds: int = 3 * 60 * 60
    max_concurrency: int = 8
    per_host_concurrency: int = 4
    # Minimum gap between request starts to one host, for careers pages and other sites whose
    # host comes from data. Documented job board APIs aren't paced.
    per_host_interval_seconds: float = 1.0
    timeout_seconds: float = 15.0
    max_retries: int = 2
    log_level: str = "INFO"
    user_agent: str = f"jobboard-mcp/{__version__} (+{REPO_URL})"
    watchlist_readonly: bool = False  # used by the M2 watchlist tools
    # Accept careers URLs from the client. Off means careers pages come only from the watchlist.
    allow_custom_urls: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        d = cls()
        return cls(
            watchlist_path=Path(os.environ.get("WATCHLIST_PATH") or d.watchlist_path),
            cache_ttl_seconds=_int("CACHE_TTL_SECONDS", d.cache_ttl_seconds),
            max_concurrency=_int("MAX_CONCURRENCY", d.max_concurrency),
            per_host_concurrency=_int("PER_HOST_CONCURRENCY", d.per_host_concurrency),
            per_host_interval_seconds=float(
                os.environ.get("PER_HOST_INTERVAL_SECONDS") or d.per_host_interval_seconds
            ),
            timeout_seconds=float(os.environ.get("HTTP_TIMEOUT_SECONDS") or d.timeout_seconds),
            log_level=(os.environ.get("LOG_LEVEL") or d.log_level).upper(),
            watchlist_readonly=_bool("WATCHLIST_READONLY", d.watchlist_readonly),
            allow_custom_urls=_bool("ALLOW_CUSTOM_URLS", d.allow_custom_urls),
        )


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update(fields)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("jobboard_mcp")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False
    # httpx logs every request at INFO; our own http/board logs already cover it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
