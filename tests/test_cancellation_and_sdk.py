import asyncio

import pytest
from fastmcp import FastMCP
from telethon import TelegramClient, errors, functions, types
from telethon.sessions import StringSession

from zai_telegram.adapter import TelegramAdapter


async def test_embedded_mcp_transport_cancels_upstream_tool_and_leaves_no_running_factory(config):
    entered, released = asyncio.Event(), asyncio.Event()
    server = FastMCP("offline cancellation fixture")

    @server.tool
    async def send_message(account: str, chat_id: str, message: str) -> dict:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()
        return {}

    adapter = TelegramAdapter(config.secret_path, write_enabled=True)

    async def embedded():
        return server

    adapter._embedded_server = embedded
    task = asyncio.create_task(
        adapter.call_write(
            "send_message",
            {
                "account": "default",
                "chat_id": "101",
                "message": "fixture",
            },
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(released.wait(), 5)


async def test_pinned_telethon_reuses_same_send_random_id_across_rpc_retry():
    client = TelegramClient(StringSession(), 12345, "synthetic-api-hash", request_retries=1)
    request = functions.messages.SendMessageRequest(peer=types.InputPeerSelf(), message="fixture")
    attempts = []

    class Sender:
        def send(self, value, **kwargs):
            attempts.append((value, value.random_id))
            future = asyncio.get_running_loop().create_future()
            if len(attempts) == 1:
                future.set_exception(errors.ServerError(request=value, message="synthetic"))
            else:
                future.set_result(True)
            return future

    assert await client._call(Sender(), request) is True
    assert len(attempts) == 2
    assert attempts[0][0] is attempts[1][0] is request
    assert attempts[0][1] == attempts[1][1]


async def test_pinned_telethon_cancellation_does_not_start_new_send_attempt():
    client = TelegramClient(StringSession(), 12345, "synthetic-api-hash", request_retries=3)
    request = functions.messages.SendMessageRequest(peer=types.InputPeerSelf(), message="fixture")
    entered, futures = asyncio.Event(), []

    class Sender:
        def send(self, value, **kwargs):
            future = asyncio.get_running_loop().create_future()
            futures.append(future)
            entered.set()
            return future

    task = asyncio.create_task(client._call(Sender(), request))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert len(futures) == 1 and futures[0].cancelled()
