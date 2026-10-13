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


async def test_hot_activation_on_multi_account_server(config, monkeypatch, tmp_path):
    bindings = tmp_path / "bindings.json"
    bindings.write_text(json.dumps({"local-operator": ["default", "second"]}))
    bindings.chmod(0o600)
    monkeypatch.setenv("TELEGRAM_BINDINGS_FILE", str(bindings))
    activated = []

    async def activate(label):
        activated.append(label)
        return True

    settings = replace(config, bindings={"local-operator": ["default", "second"]})
    server, _ = make_server(settings, monkeypatch)
    import zai_telegram.server as srv

    adapter = FixtureAdapter()
    adapter.activate_account = activate
    surface = build_surface("default,account_admin")
    server = create_server(settings, transport="stdio", adapter=adapter, surface=surface)
    async with Client(server) as client:
        await client.call_tool(ADD, {"label": "work"})
        done = (await client.call_tool(ADD, {"label": "work", "action": "status"})).data
        assert done["restart_required"] is False and done["active"] and activated == ["work"]
        listed = (await client.call_tool("telegram_list_accounts", {})).data
        assert listed["accounts"] == ["default", "second", "work"]
    assert srv  # module patched by make_server


async def test_single_account_server_saves_but_needs_restart(config, monkeypatch):
    async def activate(label):
        return True

    server, _ = make_server(config, monkeypatch)
    async with Client(server) as client:
        await client.call_tool(ADD, {"label": "work"})
        done = (await client.call_tool(ADD, {"label": "work", "action": "status"})).data
    assert done["restart_required"] is True


async def test_adapter_hot_activation_requires_pure_addition(config, monkeypatch):
    import zai_telegram.adapter as module
    from zai_telegram.adapter import TelegramAdapter
    from zai_telegram.transport import ProviderError

    adapter = TelegramAdapter(config.secret_path, write_enabled=True)
    base = {k: v for k, v in adapter._secret().items() if adapter._allowed_secret_key(k)}

    class NewClient:
        async def connect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def disconnect(self):
            pass

    runtime = SimpleNamespace(clients={"default": object()}, _build_client=lambda s, label: NewClient())
    adapter._runtime = runtime
    monkeypatch.setattr(module, "_PROCESS_FINGERPRINT", adapter._fingerprint(base))
    monkeypatch.setattr(module.os, "environ", {})
    import telethon.sessions

    monkeypatch.setattr(telethon.sessions, "StringSession", lambda value: value)
    account_login.store_session(config.secret_path, "work", "synthetic-new-session")
    assert await adapter.activate_account("work") is True and "work" in runtime.clients
    # The next load check sees the new file and the updated fingerprint: no restart error.
    full = {k: v for k, v in adapter._secret().items() if adapter._allowed_secret_key(k)}
    assert adapter._fingerprint(full) == module._PROCESS_FINGERPRINT
    # Any other change still requires a restart.
    text = config.secret_path.read_text().replace("synthetic-api-hash", "changed-hash")
    config.secret_path.write_text(text)
    account_login.store_session(config.secret_path, "more", "synthetic-more")
    with pytest.raises(ProviderError):
        await adapter.activate_account("more")
