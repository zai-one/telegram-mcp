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


async def add_account(
    secret_path: Path,
    label: str,
    *,
    bindings_path: Path | None = None,
    actor: str = "local-operator",
    use_qr: bool = False,
) -> bool:
    """Log in one more account locally and add it under ``label`` to existing files.

    Phone login (code) or QR login; a 2FA password is read with getpass in this
    terminal only. The session goes to ``TELEGRAM_SESSION_STRING_<LABEL>`` in the
    existing private secret file (never overwritten); the label is granted to
    ``actor`` in the bindings file. Returns True when the binding was updated.
    """
    from zai_telegram.account_login import LABEL, grant_label, qr_text, store_session
    from zai_telegram.secrets import read_private_secret_env

    if LABEL.fullmatch(label) is None or label == "default":
        raise ValueError("label must match [a-z][a-z0-9_]{0,31} and not be 'default'")
    secret = await asyncio.to_thread(read_private_secret_env, secret_path)
    if secret.get(f"TELEGRAM_SESSION_STRING_{label.upper()}"):
        raise ValueError("account label already configured")
    api_id, api_hash = int(secret["TELEGRAM_API_ID"]), secret["TELEGRAM_API_HASH"]
    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.connect()
        try:
            if use_qr:
                qr = await client.qr_login()
                while True:
                    print(qr_text(qr.url))
                    print("Scan in Telegram: Settings > Devices > Link Desktop Device (waiting 60 s)...")
                    try:
                        await qr.wait(timeout=60)
                        break
                    except TimeoutError:
                        await qr.recreate()
            else:
                phone = (
                    await asyncio.to_thread(getpass.getpass, "Phone number of the new account: ")
                ).strip()
                sent = await client.send_code_request(phone)
                code = (await asyncio.to_thread(getpass.getpass, "Telegram login code: ")).strip()
                await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            password = await asyncio.to_thread(getpass.getpass, "Telegram two-step verification password: ")
            await client.sign_in(password=password)
        session = client.session.save()
        if not session:
            raise ValueError("empty session")
        await asyncio.to_thread(store_session, secret_path, label, session)
    finally:
        await client.disconnect()
    return await asyncio.to_thread(grant_label, bindings_path, actor, label)
