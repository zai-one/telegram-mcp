"""Accounts added while the server runs (terminal --add-account / in chat). Mocks only."""

import json
import os
from types import SimpleNamespace

import pytest
from fastmcp import Client
from test_runtime import FixtureAdapter

import zai_telegram.adapter as module
from zai_telegram.account_login import store_session
from zai_telegram.adapter import TelegramAdapter
from zai_telegram.server import create_server
from zai_telegram.transport import ProviderError


class EchoAdapter(FixtureAdapter):
    leak = ""

    def has_account(self, label):
        return label in {"default", "second", "work"}

    async def call(self, tool, args):
        self.calls.append((tool, args))
        return {"results": [{"id": 101, "name": "Fixture", "note": self.leak}]}


def bump(path):
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))


async def test_bindings_change_enables_account_selection_and_redaction(config, monkeypatch, tmp_path):
    bindings = tmp_path / "bindings.json"
    bindings.write_text(json.dumps({"local-operator": "default"}))
    bindings.chmod(0o600)
    monkeypatch.setenv("TELEGRAM_BINDINGS_FILE", str(bindings))
    adapter = EchoAdapter()
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert "account" not in tools["telegram_get_chat"].input_schema["properties"]
        # Terminal `--add-account work` while the server runs: secret + bindings change.
        store_session(config.secret_path, "work", "synthetic-work-session")
        bindings.write_text(json.dumps({"local-operator": ["default", "work"]}))
        bump(bindings)
        bump(config.secret_path)
        adapter.leak = "synthetic-work-session"
        result = (await client.call_tool("telegram_list_chats", {"limit": 5})).data
        assert "synthetic-work-session" not in json.dumps(result)
        tools = {t.name: t for t in await client.list_tools()}
        assert "account" in tools["telegram_get_chat"].input_schema["properties"]
        listed = (await client.call_tool("telegram_list_accounts", {})).data
        assert listed["accounts"] == ["default", "work"]
        await client.call_tool("telegram_get_chat", {"chat_id": "101", "account": "work"})
        assert adapter.calls[-1][1]["account"] == "work"


async def test_invalid_bindings_file_keeps_previous_grants(config, monkeypatch, tmp_path):
    bindings = tmp_path / "bindings.json"
    bindings.write_text(json.dumps({"local-operator": "default"}))
    bindings.chmod(0o600)
    monkeypatch.setenv("TELEGRAM_BINDINGS_FILE", str(bindings))
    async with Client(create_server(config, transport="stdio", adapter=EchoAdapter())) as client:
        bindings.write_text("{not json")
        bump(bindings)
        await client.call_tool("telegram_list_chats", {"limit": 5})
        tools = {t.name for t in await client.list_tools()}
        assert "telegram_list_accounts" not in tools


async def test_loaded_runtime_hot_adds_pure_session_addition(config, monkeypatch):
    import telethon.sessions

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
    adapter._runtime, adapter._server, adapter._active_secret = runtime, object(), dict(base)
    monkeypatch.setattr(module, "_PROCESS_FINGERPRINT", adapter._fingerprint(base))
    monkeypatch.setattr(module.os, "environ", {})
    monkeypatch.setattr(telethon.sessions, "StringSession", lambda value: value)
    store_session(config.secret_path, "work", "synthetic-work-session")
    assert await adapter._embedded_server() is adapter._server
    assert "work" in runtime.clients
    # A changed API hash is never hot-applied.
    text = config.secret_path.read_text().replace("synthetic-api-hash", "changed")
    config.secret_path.write_text(text)
    with pytest.raises(ProviderError, match="restart required"):
        await adapter._embedded_server()
