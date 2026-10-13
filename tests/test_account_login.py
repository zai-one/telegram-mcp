"""In-chat QR add-account: stdio only, no secrets returned, never overwrites, 2FA stays local. Mocks only."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from telethon.errors import SessionPasswordNeededError
from test_runtime import FixtureAdapter

from zai_telegram import account_login
from zai_telegram.secrets import read_secret_env
from zai_telegram.server import create_server
from zai_telegram.toolsets import build_surface

ADD = "telegram_add_account"


class FakeQR:
    def __init__(self, outcome):
        self.url = "tg://login?token=synthetic"
        self.expires = datetime.now(UTC) + timedelta(seconds=30)
        self.outcome = outcome

    async def wait(self, timeout):  # noqa: ASYNC109 - mirrors Telethon QRLogin.wait
        if self.outcome is not None:
            raise self.outcome

    async def recreate(self):
        pass


class FakeClient:
    def __init__(self, outcome):
        self.qr = FakeQR(outcome)
        self.session = SimpleNamespace(save=lambda: "synthetic-new-session")
        self.disconnected = False

    async def connect(self):
        pass

    async def qr_login(self):
        return self.qr

    async def disconnect(self):
        self.disconnected = True


def make_server(config, monkeypatch, outcome=None):
    clients = []

    def factory(api_id, api_hash):
        clients.append(FakeClient(outcome))
        return clients[-1]

    monkeypatch.setattr(account_login, "_client_factory", factory)
    import zai_telegram.server as srv

    monkeypatch.setattr(
        srv, "register_account_login", lambda s, r: account_login.register_account_login(s, r, factory)
    )
    surface = build_surface("default,account_admin")
    return create_server(config, transport="stdio", adapter=FixtureAdapter(), surface=surface), clients


async def test_opt_in_only(config):
    async with Client(create_server(config, transport="stdio", adapter=FixtureAdapter())) as client:
        assert ADD not in {t.name for t in await client.list_tools()}


async def test_qr_flow_saves_session_privately_and_grants_label(config, monkeypatch, tmp_path):
    bindings = tmp_path / "bindings.json"
    bindings.write_text(json.dumps({"local-operator": "default"}))
    bindings.chmod(0o600)
    monkeypatch.setenv("TELEGRAM_BINDINGS_FILE", str(bindings))
    server, clients = make_server(config, monkeypatch)
    async with Client(server) as client:
        started = (await client.call_tool(ADD, {"label": "work"})).data
        assert started["status"] == "scan_qr" and any(ch in started["qr"] for ch in "█▀▄")
        done = (await client.call_tool(ADD, {"label": "work", "action": "status"})).data
        assert done["status"] == "added" and done["restart_required"]
        assert "synthetic-new-session" not in json.dumps(done)
        with pytest.raises(ToolError):
            await client.call_tool(ADD, {"label": "work"})  # never overwrite
    assert read_secret_env(config.secret_path)["TELEGRAM_SESSION_STRING_WORK"] == "synthetic-new-session"
    assert read_secret_env(config.secret_path)["TELEGRAM_SESSION_STRING"] == "synthetic-session"
    assert json.loads(bindings.read_text())["local-operator"] == ["default", "work"]
    assert clients[0].disconnected


async def test_two_factor_never_asks_for_password_in_chat(config, monkeypatch):
    server, _ = make_server(config, monkeypatch, SessionPasswordNeededError(request=None))
    async with Client(server) as client:
        await client.call_tool(ADD, {"label": "work"})
        result = (await client.call_tool(ADD, {"label": "work", "action": "status"})).data
    assert result["status"] == "password_required" and "never" in result["next"].lower()
    assert "TELEGRAM_SESSION_STRING_WORK" not in read_secret_env(config.secret_path)


async def test_waiting_status_without_start_and_bad_label(config, monkeypatch):
    server, _ = make_server(config, monkeypatch, TimeoutError())
    async with Client(server) as client:
        assert (await client.call_tool(ADD, {"label": "x", "action": "status"})).data[
            "status"
        ] == "not_started"
        await client.call_tool(ADD, {"label": "x"})
        assert (await client.call_tool(ADD, {"label": "x", "action": "status"})).data["status"] == "waiting"
        assert (await client.call_tool(ADD, {"label": "x", "action": "cancel"})).data["status"] == "cancelled"
        for bad in ("Bad", "1x", "a" * 40):
            with pytest.raises(ToolError):
                await client.call_tool(ADD, {"label": bad})


async def test_read_only_scope_hides_login(config, monkeypatch):
    server, _ = make_server(replace(config, local_scopes=frozenset({"telegram:read"})), monkeypatch)
    async with Client(server) as client:
        assert ADD not in {t.name for t in await client.list_tools()}
