from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.resources import files
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP

from zai_telegram.adapter import TELEGRAM_WRITE_ALLOWLIST, TelegramAdapter
from zai_telegram.transport import ProviderError
from zai_telegram.vendor_integrity import assert_pinned_vendor


def test_private_upstream_matches_pinned_manifest_and_keeps_attribution():
    assert_pinned_vendor()
    root = files("zai_telegram").joinpath("_vendor")
    manifest = json.loads(root.joinpath("manifest.json").read_text())
    assert manifest["commit"] == "f1a2d8e00a7f127bb7702655c58fdfcee7e73a5a"
    assert "Apache License" in root.joinpath("LICENSE").read_text()


async def test_different_config_mode_and_ambient_env_cannot_reuse_first_runtime(config, monkeypatch):
    import zai_telegram.adapter as module

    server = FastMCP("fixture-upstream")
    runtime = SimpleNamespace(mcp=server, _apply_exposed_tools_mode=lambda *args: None)
    imports = []

    def fake_import(name):
        imports.append(name)
        return runtime

    monkeypatch.setattr(module, "_PROCESS_FINGERPRINT", None)
    monkeypatch.setattr(module, "importlib", SimpleNamespace(import_module=fake_import))
    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(
            environ={
                "TELEGRAM_SESSION_STRING_OTHER": "ambient-session",
                "TELEGRAM_PROXY_HOST": "untrusted-host",
                "TELEGRAM_LOG_FILE": "untrusted.log",
                "PATH": os.environ["PATH"],
            }
        ),
    )
    first = TelegramAdapter(config.secret_path, strict_secret=True)
    assert await first._embedded_server() is server
    assert "TELEGRAM_SESSION_STRING_OTHER" not in module.os.environ
    assert "TELEGRAM_PROXY_HOST" not in module.os.environ
    assert "TELEGRAM_LOG_FILE" not in module.os.environ
    assert module.os.environ["PYTHON_DOTENV_DISABLED"] == "1"
    with pytest.raises(ProviderError, match="restart"):
        await TelegramAdapter(config.secret_path, write_enabled=True)._embedded_server()
    config.secret_path.write_text(config.secret_path.read_text().replace("synthetic-session", "changed"))
    with pytest.raises(ProviderError, match="restart"):
        await first._embedded_server()
    assert len(imports) == 2


@pytest.mark.parametrize("write", [False, True])
def test_real_pinned_upstream_loads_and_discovers_without_network(tmp_path, write):
    script = tmp_path / "offline_upstream.py"
    script.write_text(
        """
import asyncio
import socket
from pathlib import Path
from telethon.crypto import AuthKey
from telethon.sessions import StringSession
from fastmcp import Client
from zai_telegram.adapter import TelegramAdapter, TELEGRAM_WRITE_ALLOWLIST

original_connect = socket.socket.connect
original_connect_ex = socket.socket.connect_ex
def local_only(original):
    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("external network forbidden in upstream discovery")
        if isinstance(address, tuple) and address[1] == 443:
            raise AssertionError("fixture Telegram DC must never be contacted")
        return original(sock, address)
    return connect
socket.socket.connect = local_only(original_connect)
socket.socket.connect_ex = local_only(original_connect_ex)
session = StringSession()
session.set_dc(2, "127.0.0.1", 443)
session.auth_key = AuthKey(data=b"\\x01" * 256)
secret = Path(__file__).with_suffix(".env")
secret.write_text("TELEGRAM_API_ID=12345\\nTELEGRAM_API_HASH=synthetic-api-hash\\n"
                  "TELEGRAM_SESSION_STRING=" + session.save() + "\\n")
secret.chmod(0o600)

async def main():
    adapter = TelegramAdapter(secret, write_enabled=__WRITE_ENABLED__, strict_secret=True)
    server = await adapter._embedded_server()
    async with Client(server) as client:
        tools = await client.list_tools()
    names = {tool.name for tool in tools}
    assert {"list_chats", "get_chat", "get_messages", "search_global"} <= names
    writes = {tool.name for tool in tools if not tool.annotations or not tool.annotations.read_only_hint}
    assert writes <= set(TELEGRAM_WRITE_ALLOWLIST)
    assert bool(writes) == __WRITE_ENABLED__
    assert "send_message" in names if __WRITE_ENABLED__ else "send_message" not in names
    await adapter.close()
    print("pinned upstream discovery passed", len(tools), "write", __WRITE_ENABLED__)

asyncio.run(main())
""".replace("__WRITE_ENABLED__", str(write)),
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_write_allowlist_is_frozen():
    assert "delete_chat_history" in TELEGRAM_WRITE_ALLOWLIST
    assert TELEGRAM_WRITE_ALLOWLIST["delete_chat_history"] == "high"
    assert "unexpected_new_write" not in TELEGRAM_WRITE_ALLOWLIST
