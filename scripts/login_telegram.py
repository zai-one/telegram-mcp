"""Local interactive login; save a Telethon StringSession without printing it."""

from __future__ import annotations

import asyncio
import getpass
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession


async def create_session(destination: Path) -> None:
    if await asyncio.to_thread(destination.exists):
        raise ValueError("session file already exists")
    api_id = int((await asyncio.to_thread(input, "Telegram application API ID: ")).strip())
    api_hash = (await asyncio.to_thread(getpass.getpass, "Telegram application API hash: ")).strip()
    phone = (await asyncio.to_thread(getpass.getpass, "Your Telegram phone number: ")).strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        code = (await asyncio.to_thread(getpass.getpass, "Telegram login code: ")).strip()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            password = await asyncio.to_thread(getpass.getpass, "Telegram two-step verification password: ")
            await client.sign_in(password=password)
        session = client.session.save()
        if not session:
            raise ValueError("empty session")
        await asyncio.to_thread(_save_session, destination, api_id, api_hash, session)
    finally:
        await client.disconnect()


def _save_session(destination: Path, api_id: int, api_hash: str, session: str) -> None:
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(f"TELEGRAM_API_ID={api_id}\nTELEGRAM_API_HASH={api_hash}\n")
        stream.write(f"TELEGRAM_SESSION_STRING={session}\n")
