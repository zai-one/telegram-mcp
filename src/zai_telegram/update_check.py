"""Suggest-only update check against public GitHub releases of zai-one/telegram-mcp.

One unauthenticated HTTPS GET with a short timeout, cached for 24 hours.
Offline, rate-limited or malformed answers degrade to ``status="unavailable"``
(plus the last good result, if any). Every attempt is recorded in the cache: a
failed attempt is not repeated for 30 minutes, and ``force`` is honoured only
when the last attempt is at least 60 seconds old. Only plain version tags
(``v1.2.3``) and URLs of this repository's releases are ever reported.
Nothing is ever downloaded or installed; the result only names the command the
operator may run. Set ``TELEGRAM_MCP_DISABLE_UPDATE_CHECK=1`` to turn the check off.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from zai_telegram import __version__

REPO = "zai-one/telegram-mcp"
DISTRIBUTION = "zai-telegram-mcp"
DISABLE_ENV = "TELEGRAM_MCP_DISABLE_UPDATE_CHECK"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
CACHE_TTL_SECONDS = 24 * 60 * 60
FAILURE_BACKOFF_SECONDS = 30 * 60
FORCE_MIN_INTERVAL_SECONDS = 60
TIMEOUT_SECONDS = 3.0
MAX_RESPONSE_BYTES = 1_048_576
_VERSION_TEXT = r"v?(\d{1,6}(?:\.\d{1,6}){0,3})"
_VERSION = re.compile(_VERSION_TEXT)
_RELEASE_PAGE = re.compile(rf"https://github\.com/{re.escape(REPO)}/releases/tag/{_VERSION_TEXT}")
_WHEEL = re.compile(
    rf"https://github\.com/{re.escape(REPO)}/releases/download/{_VERSION_TEXT}/[A-Za-z0-9_.+-]{{1,200}}\.whl"
)
# One fetch decision and cache write at a time inside this process.
_LOCK = threading.Lock()
_RESULT_KEYS = ("checked_at", "latest_version", "release_url", "wheel_url")

Fetcher = Callable[[str, float], bytes]


def disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def parse_version(value: Any) -> tuple[int, ...] | None:
    """Parse a plain ``v?N(.N){0,3}`` tag; anything else (spaces, suffixes) is not a release."""
    if not isinstance(value, str):
        return None
    match = _VERSION.fullmatch(value)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _is_newer(latest: str, current: str) -> bool | None:
    left, right = parse_version(latest), parse_version(current)
    if left is None or right is None:
        return None
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def cache_path() -> Path:
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_CACHE_HOME"):
        base = Path(os.environ["XDG_CACHE_HOME"])
    else:
        base = Path.home() / ".cache"
    return base / DISTRIBUTION / "update-check.json"


def _timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _trusted(pattern: re.Pattern[str], value: Any) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _read_cache() -> dict[str, Any] | None:
    """Return validated cache fields; a bad last result is dropped, attempt times are kept."""
    try:
        path = cache_path()
        if path.stat().st_size > 65_536:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    record: dict[str, Any] = {
        "attempted_at": _timestamp(data.get("attempted_at")),
        "failed_at": _timestamp(data.get("failed_at")),
        "checked_at": None,
        "latest_version": None,
        "release_url": None,
        "wheel_url": None,
    }
    checked_at, version = _timestamp(data.get("checked_at")), parse_version(data.get("latest_version"))
    if checked_at is not None and version is not None:
        record.update(
            checked_at=checked_at,
            latest_version=".".join(str(part) for part in version),
            release_url=_trusted(_RELEASE_PAGE, data.get("release_url")),
            wheel_url=_trusted(_WHEEL, data.get("wheel_url")),
        )
    if record["attempted_at"] is None:
        record["attempted_at"] = record["checked_at"]
    return record


def _has_result(record: dict[str, Any] | None) -> bool:
    return record is not None and record.get("latest_version") is not None


def _within(stamp: float | None, moment: float, window: float) -> bool:
    return stamp is not None and 0 <= moment - stamp < window


def _fetch_due(record: dict[str, Any] | None, moment: float, *, force: bool) -> bool:
    """Decide whether one GitHub request is allowed now (shared by tool and startup refresh)."""
    if record is None:
        return True
    if force:
        return not _within(record["attempted_at"], moment, FORCE_MIN_INTERVAL_SECONDS)
    if _has_result(record) and _within(record["checked_at"], moment, CACHE_TTL_SECONDS):
        return False
    return not _within(record["failed_at"], moment, FAILURE_BACKOFF_SECONDS)


def _write_cache(record: dict[str, Any]) -> None:
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


def _default_fetch(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"{DISTRIBUTION}/{__version__} update-check",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
        return response.read(MAX_RESPONSE_BYTES + 1)


def _parse_release(body: bytes) -> dict[str, Any]:
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("release response too large")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("unexpected release response")
    tag = data.get("tag_name")
    version = parse_version(tag)
    if version is None:
        raise ValueError("release tag is not a version")
    latest = ".".join(str(part) for part in version)
    # ``tag`` passed parse_version, so it is a plain version and safe in a URL.
    release_url = _trusted(_RELEASE_PAGE, data.get("html_url")) or f"{RELEASES_URL}/tag/{tag}"
    wheel_url = None
    assets = data.get("assets")
    for asset in assets if isinstance(assets, list) else []:
        url = _trusted(_WHEEL, asset.get("browser_download_url") if isinstance(asset, dict) else None)
        if url is not None:
            wheel_url = url
            break
    return {"latest_version": latest, "release_url": release_url, "wheel_url": wheel_url}


def _install_kind() -> str:
    """Return ``git`` for a source checkout, otherwise ``package``."""
    root = Path(__file__).resolve().parents[2]
    return "git" if (root / ".git").exists() and (root / "pyproject.toml").is_file() else "package"


def update_command(latest: str | None, wheel_url: str | None) -> str:
    if _install_kind() == "git":
        return "git pull --ff-only && uv sync --frozen  (run inside your telegram-mcp checkout)"
    if wheel_url:
        return f'uv tool install --force "{DISTRIBUTION} @ {wheel_url}"'
    return f'uv tool install --force "{DISTRIBUTION} @ git+https://github.com/{REPO}@v{latest}"'


def check_for_update(
    *, force: bool = False, fetch: Fetcher | None = None, now: float | None = None
) -> dict[str, Any]:
    """Compare the installed version with the latest GitHub release. Never raises."""
    current = __version__
    result: dict[str, Any] = {
        "current_version": current,
        "latest_version": None,
        "update_available": None,
        "status": "unavailable",
        "source": None,
        "checked_at": None,
        "release_notes_url": RELEASES_URL,
        "update_command": None,
        "auto_update": False,
        "disable_with": f"{DISABLE_ENV}=1",
    }
    if disabled():
        result["status"] = "disabled"
        return result
    moment = time.time() if now is None else now
    with _LOCK:
        record = _read_cache()
        if _fetch_due(record, moment, force=force):
            try:
                fetched = _parse_release((fetch or _default_fetch)(LATEST_RELEASE_API, TIMEOUT_SECONDS))
            except Exception:  # offline, HTTP error, rate limit, malformed JSON: degrade quietly
                fetched = None
            if fetched is not None:
                record = {"checked_at": moment, **fetched, "attempted_at": moment, "failed_at": None}
                result["source"] = "github"
            else:
                last_good = {key: record[key] for key in _RESULT_KEYS} if _has_result(record) else {}
                record = {**last_good, "attempted_at": moment, "failed_at": moment}
                result["source"] = "stale_cache" if last_good else None
            _write_cache(record)
        elif _has_result(record):
            fresh = _within(record["checked_at"], moment, CACHE_TTL_SECONDS)
            result["source"] = "cache" if fresh else "stale_cache"
    if not _has_result(record):
        return result
    newer = _is_newer(record["latest_version"], current)
    result.update(
        latest_version=record["latest_version"],
        update_available=newer,
        status="update_available" if newer else "up_to_date",
        checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record["checked_at"])),
        release_notes_url=record.get("release_url") or RELEASES_URL,
        update_command=update_command(record["latest_version"], record.get("wheel_url")) if newer else None,
    )
    return result


def cached_update_hint() -> str | None:
    """One-line startup hint from the cache only; never touches the network."""
    if disabled():
        return None
    record = _read_cache()
    if not _has_result(record) or not _is_newer(record["latest_version"], __version__):
        return None
    return (
        f"Update available: {DISTRIBUTION} {__version__} -> {record['latest_version']}. "
        f"Call telegram_check_update for release notes and the update command; "
        "nothing is installed automatically."
    )


def refresh_in_background() -> None:
    """Refresh a stale cache on a daemon thread so startup never waits on the network.

    Obeys the same 24 h cache and 30 min failure backoff as ``check_for_update``.
    """
    if disabled() or not _fetch_due(_read_cache(), time.time(), force=False):
        return
    threading.Thread(target=check_for_update, name="update-check", daemon=True).start()
