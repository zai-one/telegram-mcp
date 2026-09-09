from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from fastmcp import FastMCP
from pydantic import BaseModel

from zai_telegram.adapter import TelegramAdapter, _json_safe_result
from zai_telegram.transport import (
    ProviderError,
    ProviderTimeoutError,
    ProviderTransportError,
)


def _adapter(tmp_path: Path, *, write_enabled: bool = False) -> TelegramAdapter:
    secret = tmp_path / "telegram.env"
    secret.write_text(
        "\n".join(
            [
                "TELEGRAM_API_ID=1",
                "TELEGRAM_API_HASH=hash",
                "TELEGRAM_SESSION_STRING=session",
            ]
        ),
        encoding="utf-8",
    )
    return TelegramAdapter(secret, write_enabled=write_enabled)


def _fail_with(adapter: TelegramAdapter, exc: BaseException) -> None:
    async def broken_server() -> Any:
        raise exc

    adapter._embedded_server = broken_server  # type: ignore[method-assign]


READ_ARGS = {"account": "default", "limit": 5}
WRITE_ARGS = {"account": "default", "chat_id": "1", "message": "hi"}


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (ConnectionError("no route to host"), ProviderTransportError),
        (OSError("network is unreachable"), ProviderTransportError),
        (TimeoutError("timed out"), ProviderTimeoutError),
        (RuntimeError("unexpected"), ProviderError),
    ],
)
async def test_read_maps_runtime_failures_into_the_error_taxonomy(
    tmp_path: Path, raised: BaseException, expected: type[ProviderError]
) -> None:
    """Unmapped failures would escape the safe envelope and skip the circuit."""
    adapter = _adapter(tmp_path)
    _fail_with(adapter, raised)
    with pytest.raises(expected):
        await adapter.call("telegram_list_chats", READ_ARGS)


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (ConnectionError("no route to host"), ProviderTransportError),
        (TimeoutError("timed out"), ProviderTimeoutError),
    ],
)
async def test_write_maps_runtime_failures_into_the_error_taxonomy(
    tmp_path: Path, raised: BaseException, expected: type[ProviderError]
) -> None:
    adapter = _adapter(tmp_path, write_enabled=True)
    _fail_with(adapter, raised)
    with pytest.raises(expected):
        await adapter.call_write("send_message", WRITE_ARGS)


async def test_typed_provider_errors_pass_through_unchanged(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    _fail_with(adapter, ProviderError("embedded Telegram runtime secret is incomplete"))
    with pytest.raises(ProviderError, match="secret is incomplete"):
        await adapter.call("telegram_list_chats", READ_ARGS)


class _Chat(BaseModel):
    chat_id: int
    title: str


async def test_read_path_returns_json_safe_values(tmp_path: Path) -> None:
    """The read path must flatten structured output exactly like the write path."""
    server: Any = FastMCP("telegram-double")

    @server.tool
    def list_chats(account: str, limit: int) -> _Chat:
        return _Chat(chat_id=limit, title=account)

    adapter = _adapter(tmp_path)
    adapter._server = server

    result = await adapter.call("telegram_list_chats", READ_ARGS)

    assert result == {"chat_id": 5, "title": "default"}


async def test_read_path_unwraps_fastmcp_scalar_result_model(tmp_path: Path) -> None:
    """Vendor JSON strings must not retain FastMCP 3's outer result wrapper."""
    server: Any = FastMCP("telegram-string-double")

    @server.tool
    def list_chats(account: str, limit: int) -> str:
        return f'{{"results":[{{"chat_id":{limit},"title":"{account}"}}]}}'

    adapter = _adapter(tmp_path)
    adapter._server = server

    result = await adapter.call("telegram_list_chats", READ_ARGS)

    assert result == '{"results":[{"chat_id":5,"title":"default"}]}'


def test_json_safe_result_flattens_fastmcp_output_dataclasses() -> None:
    """FastMCP builds dataclasses, not Pydantic models, from a tool output schema.

    Without the dataclass branch these fall through to ``str(value)`` and the
    durable idempotency ledger stores an unparseable Python repr.
    """

    @dataclasses.dataclass
    class Root:
        chat_id: int
        title: str

    assert _json_safe_result(Root(chat_id=5, title="general")) == {
        "chat_id": 5,
        "title": "general",
    }
    assert _json_safe_result([Root(chat_id=1, title="a")]) == [{"chat_id": 1, "title": "a"}]
    # A dataclass *type* is not an instance and must not be flattened.
    assert _json_safe_result(Root) == str(Root)
