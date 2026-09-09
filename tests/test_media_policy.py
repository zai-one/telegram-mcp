import asyncio
import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from zai_telegram.adapter import TelegramAdapter
from zai_telegram.media_policy import MediaPolicy, download, verify_receipt
from zai_telegram.server import create_server
from zai_telegram.state import StateStore
from zai_telegram.transport import ProviderError


@pytest.fixture
def policy(tmp_path):
    outgoing, incoming = tmp_path / "outgoing", tmp_path / "incoming"
    outgoing.mkdir()
    incoming.mkdir()
    file = tmp_path / "media.json"
    file.write_text(json.dumps({"roots": ["outgoing"], "download_root": "incoming", "max_file_bytes": 1024}))
    file.chmod(0o600)
    return MediaPolicy.load(file)


def test_operator_policy_relative_folders_bounds_and_protected_files(tmp_path, config, policy):
    assert policy.roots == (tmp_path / "outgoing",) and policy.max_file_bytes == 1024
    file = tmp_path / "bad.json"
    for body in [
        {"roots": [str(tmp_path)]},
        {"roots": [], "max_file_bytes": True},
        {"roots": ["outgoing"], "download_root": "outgoing"},
        {"roots": "outgoing"},
    ]:
        file.write_text(json.dumps(body))
        file.chmod(0o600)
        with pytest.raises(ValueError):
            MediaPolicy.load(file, protected=(config.secret_path,))
    assert MediaPolicy.load(None).roots == ()


def test_media_paths_limits_extensions_roots_and_destination(policy, tmp_path):
    note = policy.roots[0] / "note.opus"
    note.write_bytes(b"OggSfixture")
    args = {"chat_id": "101", "file_path": "note.opus", "account": "default"}
    assert policy.validate("send_voice", args)["file_path"] == str(note)
    for changed in [
        {"file_path": "../fixture.env"},
        {"file_path": "https://example.invalid/file"},
        {"chat_id": "@someone"},
        {"chat_id": True},
        {"ctx": {}},
        {"roots": [str(tmp_path)]},
        {"file_path": ["note.opus"]},
        {"file_path": "*.opus"},
    ]:
        with pytest.raises((ProviderError, OSError)):
            policy.validate("send_voice", {**args, **changed})
    big = policy.roots[0] / "large.opus"
    big.write_bytes(b"x" * 1025)
    with pytest.raises(ProviderError):
        policy.validate("send_voice", {**args, "file_path": str(big)})
    bad = policy.roots[0] / "bad.txt"
    bad.write_text("fixture")
    with pytest.raises(ProviderError):
        policy.validate("send_voice", {**args, "file_path": str(bad)})
    with pytest.raises(ProviderError):
        MediaPolicy().validate("send_voice", args)
    a = policy.roots[0] / "a.jpg"
    a.write_bytes(b"x" * 600)
    b = policy.roots[0] / "b.jpg"
    b.write_bytes(b"y" * 600)
    with pytest.raises(ProviderError, match="total"):
        policy.validate("send_album", {"chat_id": 101, "file_paths": [str(a), str(b)]})


def test_media_hardlinks_are_denied(policy, tmp_path):
    private = tmp_path / "private.txt"
    private.write_text("not for upload")
    linked = policy.roots[0] / "linked.txt"
    os.link(private, linked)
    with pytest.raises(ProviderError):
        policy.readable(str(linked), "send_file")


def test_media_symlink_escape_is_denied(policy, tmp_path):
    private = tmp_path / "private.txt"
    private.write_text("not for upload")
    linked = policy.roots[0] / "linked.txt"
    try:
        linked.symlink_to(private)
    except OSError:
        pytest.skip("OS does not permit creating a test symlink")
    with pytest.raises(ProviderError):
        policy.readable(str(linked), "send_file")


class DownloadClient:
    def __init__(self, data=b"fixture", declared=7):
        self.data, self.declared, self.downloads = data, declared, 0

    async def get_messages(self, entity, ids):
        return SimpleNamespace(media=True, file=SimpleNamespace(size=self.declared))

    async def download_media(self, message, file):
        self.downloads += 1
        if self.data is None:
            raise asyncio.CancelledError()
        file.write(self.data)


async def test_download_exclusive_file_bounded_stream_and_failure_cleanup(policy):
    async def entity(chat, client):
        return chat

    client = DownloadClient()
    runtime = SimpleNamespace(get_client=lambda label: client, resolve_entity=entity)
    args = {"account": "default", "chat_id": "101", "message_id": 55}
    result = await download(policy, runtime, args)
    from pathlib import Path

    saved = Path(result["path"])
    assert await asyncio.to_thread(saved.read_bytes) == b"fixture" and result["bytes"] == 7
    for data, declared in [(b"x" * 10, 7), (b"x", 7), (b"fixture", 2000), (None, 7)]:
        client.data, client.declared = data, declared
        with pytest.raises((ProviderError, asyncio.CancelledError)):
            await download(policy, runtime, args)
        assert list(policy.download_root.iterdir()) == [saved]
    with pytest.raises(ProviderError):
        policy.validate("download_media", {**args, "file_path": str(saved)})


async def test_media_uses_existing_account_idempotency_and_preflight_before_ledger(config, policy):
    note = policy.roots[0] / "note.ogg"
    note.write_bytes(b"OggSfixture")
    upstream, calls = FastMCP("synthetic-media"), []

    @upstream.tool()
    async def send_voice(chat_id: str, file_path: str, account: str):
        calls.append((chat_id, file_path, account))
        return "Voice message sent to chat 101 from fixture."

    adapter = TelegramAdapter(config.secret_path, write_enabled=True, media_policy=policy)
    adapter._server = upstream
    args = {
        "tool": "send_voice",
        "arguments": {"chat_id": "101", "file_path": str(note), "account": "second"},
        "idempotency_key": "voice-once",
    }
    for _ in range(2):
        async with Client(
            create_server(replace(config, media_policy=policy), transport="stdio", adapter=adapter)
        ) as client:
            await client.call_tool("telegram_write", args)
            with pytest.raises(ToolError):
                await client.call_tool(
                    "telegram_write", {**args, "arguments": {"chat_id": "101", "file_path": "../fixture.env"}}
                )
    assert calls == [("101", str(note), "default")]
    assert len(await StateStore(config).telegram_write_list("local-operator")) == 1


@pytest.mark.parametrize(
    "tool",
    [
        "send_file",
        "send_album",
        "send_voice",
        "send_sticker",
        "set_profile_photo",
        "edit_chat_photo",
        "upload_file",
    ],
)
def test_media_refusal_is_never_a_successful_receipt(tool):
    with pytest.raises(ProviderError):
        verify_receipt(tool, "send_file is disabled because roots are unavailable")
