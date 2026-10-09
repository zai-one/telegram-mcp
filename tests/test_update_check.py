from __future__ import annotations

import json
import urllib.error

import pytest
from fastmcp import Client
from test_runtime import FixtureAdapter

from zai_telegram import __version__, update_check
from zai_telegram.server import create_server

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
    result = update_check.check_for_update(fetch=FakeGitHub(error=urllib.error.URLError("offline")))
    assert result["status"] == "unavailable" and result["update_available"] is None
    update_check.check_for_update(fetch=FakeGitHub(release()), now=1_000_000)
    stale = update_check.check_for_update(
        fetch=FakeGitHub(error=TimeoutError()), force=True, now=1_000_000 + 10
    )
    assert stale["source"] == "stale_cache" and stale["latest_version"] == "99.0.0"


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
    monkeypatch.setattr(update_check, "_default_fetch", FakeGitHub(release()))
    async with Client(create_server(config, transport="stdio", adapter=FixtureAdapter())) as client:
        tool = {tool.name: tool for tool in await client.list_tools()}["telegram_check_update"]
        assert tool.annotations.read_only_hint is True
        result = await client.call_tool("telegram_check_update", {"force": True})
    assert result.data["latest_version"] == "99.0.0" and result.data["update_available"] is True
