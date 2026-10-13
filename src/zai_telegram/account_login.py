"""In-chat add-account via Telegram QR login (opt-in toolset ``account_admin``, stdio only).

Safety model:
* No phone number, login code or 2FA password ever passes through the chat.
  The user scans a QR code with an already signed-in Telegram app and approves
  the new session there (Settings > Devices > Link Desktop Device).
* Accounts protected by a 2FA password stop at ``password_required``; finishing
  is done locally with ``zai-telegram-mcp-setup --add-account <label>`` so the password stays in a terminal.
* The session string is written straight into the private secret file (0600,
  atomic replace) under ``TELEGRAM_SESSION_STRING_<LABEL>``; it is never returned.
  An existing label is never overwritten. The bound principal is granted the new
  label in the bindings file when ``TELEGRAM_BINDINGS_FILE`` is set.
* Hot activation: on a multi-account server the new client joins the loaded
  runtime and the label is granted in the live config (no restart). A
  single-account server needs a restart to expose the ``account`` argument.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from zai_telegram.secrets import read_private_secret_env, require_private_file

LABEL = re.compile(r"[a-z][a-z0-9_]{0,31}")
STATUS_WAIT_SECONDS = 25
_PENDING_TTL = 600


def qr_text(url: str) -> str:
    import qrcode

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    out = io.StringIO()
    qr.print_ascii(out=out, invert=True)
    return out.getvalue()


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tg-", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def store_session(secret_path: Path, label: str, session: str) -> None:
    key = "TELEGRAM_SESSION_STRING" if label == "default" else f"TELEGRAM_SESSION_STRING_{label.upper()}"
    existing = read_private_secret_env(secret_path)
    if existing.get(key):
        raise ValueError("account label already configured")
    text = secret_path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    _atomic_write(secret_path, f"{text}{key}={session}\n")


def grant_label(bindings_path: Path | None, actor: str, label: str) -> bool:
    if bindings_path is None:
        return False
    bindings_path = bindings_path.resolve()
    require_private_file(bindings_path)
    data = json.loads(bindings_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("binding map required")
    current = data.get(actor)
    labels = list(current) if isinstance(current, list) else ([current] if isinstance(current, str) else [])
    if label in labels:
        return True
    if len(labels) >= 16:
        raise ValueError("too many accounts for this principal")
    data[actor] = [*labels, label]
    _atomic_write(bindings_path, json.dumps(data, indent=2) + "\n")
    return True


async def _activate(runtime: Any, actor: str, label: str, granted: bool) -> dict[str, Any]:
    """Hot-activate the saved session when possible; otherwise ask for a restart."""
    active = False
    activate = getattr(runtime.adapter, "activate_account", None)
    if label != "default" and activate is not None:
        try:
            await activate(label)
            active = True
        except Exception:  # noqa: BLE001 - never leak provider detail; fall back to restart
            active = False
    selectable = active and granted and runtime.grant_account(actor, label)
    if selectable:
        return {
            "status": "added",
            "label": label,
            "granted": True,
            "active": True,
            "restart_required": False,
            "next": f"Use account={label} now (see telegram_list_accounts).",
        }
    return {
        "status": "added",
        "label": label,
        "granted": granted,
        "active": active,
        "restart_required": True,
        "next": f"Restart the MCP server, then use account={label} (see telegram_list_accounts).",
    }


class LoginSessions:
    def __init__(self) -> None:
        self.pending: dict[str, dict[str, Any]] = {}

    async def cancel(self, actor: str) -> None:
        entry = self.pending.pop(actor, None)
        if entry:
            await entry["client"].disconnect()


def _client_factory(api_id: int, api_hash: str) -> Any:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(StringSession(), api_id, api_hash)


def register_account_login(server: Any, runtime: Any, client_factory=_client_factory) -> None:
    sessions = LoginSessions()

    @server.tool(
        auth=runtime.require_scopes("telegram:write"),
        annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    )
    async def telegram_add_account(label: str, action: str = "start") -> dict:
        """Add a Telegram account by QR login: start -> user scans -> status; no codes in chat."""
        if runtime.transport != "stdio":
            raise PermissionError("in-chat login is available only for local stdio servers")
        if action not in {"start", "status", "cancel"}:
            raise ValueError("action must be start, status or cancel")
        if not isinstance(label, str) or LABEL.fullmatch(label) is None:
            raise ValueError("label must match [a-z][a-z0-9_]{0,31}")
        actor = runtime.current_access().principal_id
        config = runtime.config
        if action == "cancel":
            await sessions.cancel(actor)
            return {"status": "cancelled", "label": label}
        if action == "start":
            await sessions.cancel(actor)
            secret = read_private_secret_env(config.secret_path)
            key = (
                "TELEGRAM_SESSION_STRING"
                if label == "default"
                else f"TELEGRAM_SESSION_STRING_{label.upper()}"
            )
            if secret.get(key):
                raise ValueError("account label already configured")
            try:
                api_id, api_hash = int(secret["TELEGRAM_API_ID"]), secret["TELEGRAM_API_HASH"]
            except (KeyError, ValueError):
                raise ValueError("TELEGRAM_API_ID/TELEGRAM_API_HASH missing in the secret file") from None
            client = client_factory(api_id, api_hash)
            await client.connect()
            qr = await client.qr_login()
            sessions.pending[actor] = {"client": client, "qr": qr, "label": label, "at": time.time()}
            return {
                "status": "scan_qr",
                "label": label,
                "qr": qr_text(qr.url),
                "expires": qr.expires.isoformat() if getattr(qr, "expires", None) else None,
                "next": (
                    "Ask the user to open Telegram on a phone where this account is signed in: "
                    "Settings > Devices > Link Desktop Device, scan the QR, then call action=status."
                ),
            }
        entry = sessions.pending.get(actor)
        if entry is None or entry["label"] != label or time.time() - entry["at"] > _PENDING_TTL:
            await sessions.cancel(actor)
            return {"status": "not_started", "label": label, "next": "call action=start"}
        from telethon.errors import SessionPasswordNeededError

        client, qr = entry["client"], entry["qr"]
        try:
            await qr.wait(timeout=STATUS_WAIT_SECONDS)
        except SessionPasswordNeededError:
            await sessions.cancel(actor)
            return {
                "status": "password_required",
                "label": label,
                "next": (
                    "This account has a 2FA password. Never type it in chat: finish locally with "
                    "`zai-telegram-mcp-setup --add-account "
                    + label
                    + " --directory <setup dir> --qr` in a terminal "
                    "(the password is typed there, hidden), then restart the MCP server."
                ),
            }
        except TimeoutError:
            if getattr(qr, "expires", None) and qr.expires.timestamp() <= time.time():
                await qr.recreate()
                return {"status": "scan_qr", "label": label, "qr": qr_text(qr.url), "refreshed": True}
            return {"status": "waiting", "label": label, "next": "scan the QR, then call action=status again"}
        session = client.session.save()
        await sessions.cancel(actor)
        if not session:
            raise ValueError("login produced no session")
        await asyncio.to_thread(store_session, config.secret_path, label, session)
        raw = os.environ.get("TELEGRAM_BINDINGS_FILE")
        granted = await asyncio.to_thread(grant_label, Path(raw) if raw else None, actor, label)
        return await _activate(runtime, actor, label, granted)
