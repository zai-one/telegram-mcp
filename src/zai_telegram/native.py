"""Native (non-vendored) Telegram capabilities on the pinned runtime's bound client.

Parity with newer upstream releases without re-pinning the vendor: each entry is a
small, reviewed Telethon call reachable only through the existing generic gates -
reads via ``telegram_read`` (TELEGRAM_READ_ALLOWLIST), writes via ``telegram_write``
(TELEGRAM_WRITE_ALLOWLIST + write categories + idempotency ledger + chat ACL/allowlist).
Every handler runs after the runtime's ``ensure_connected`` liveness check.
Results are plain JSON; message text is marked untrusted.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from zai_telegram.transport import ProviderError

_LINK = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:(c)/(\d{1,20})|([A-Za-z][A-Za-z0-9_]{3,31}))"
    r"(?:/\d{1,10})?/(\d{1,10})(?:\?.*)?$"
)


def _int(value: Any, name: str, lo: int = 1, hi: int = 2**63 - 1) -> int:
    if isinstance(value, bool):
        raise ProviderError(f"Telegram {name} must be an integer")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ProviderError(f"Telegram {name} must be an integer") from None
    if not lo <= number <= hi:
        raise ProviderError(f"Telegram {name} is out of range")
    return number


def _chat(value: Any) -> int | str:
    text = str(value if value is not None else "").strip()
    if not 1 <= len(text) <= 128:
        raise ProviderError("Telegram chat_id is required")
    return int(text) if text.lstrip("-").isdigit() else text


def message_row(message: Any) -> dict[str, Any]:
    text = getattr(message, "message", None) or ""
    date = getattr(message, "date", None)
    return {
        "message_id": getattr(message, "id", None),
        "chat_id": getattr(message, "chat_id", None),
        "sender_id": getattr(message, "sender_id", None),
        "date": date.isoformat() if isinstance(date, datetime) else None,
        "text": text[:4096],
        "text_truncated": len(text) > 4096,
        "outgoing": bool(getattr(message, "out", False)),
        "has_media": bool(getattr(message, "media", None)),
        "reply_to_message_id": getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
        "untrusted_content": True,
    }


def parse_message_link(link: Any) -> tuple[int | str, int]:
    if not isinstance(link, str) or len(link) > 256:
        raise ProviderError("Telegram link must be a t.me message link")
    match = _LINK.match(link.strip())
    if match is None:
        raise ProviderError("Telegram link must be a t.me message link")
    private, internal, username, message_id = match.groups()
    chat: int | str = int(f"-100{internal}") if private else username
    return chat, int(message_id)


# ---------------------------------------------------------------- reads


async def get_message_by_link(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    chat, message_id = parse_message_link(args.get("link"))
    entity = await client.get_entity(chat)
    message = await client.get_messages(entity, ids=message_id)
    if message is None:
        raise ProviderError("Telegram message not found")
    return {"chat_id": getattr(entity, "id", None), "message": message_row(message)}


async def export_unread_messages(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    max_chats = _int(args.get("max_chats", 10), "max_chats", 1, 50)
    per_chat = _int(args.get("per_chat", 20), "per_chat", 1, 100)
    chats = []
    async for dialog in client.iter_dialogs(limit=200):
        unread = int(getattr(dialog, "unread_count", 0) or 0)
        if unread <= 0:
            continue
        messages = await client.get_messages(dialog.entity, limit=min(unread, per_chat))
        chats.append(
            {
                "chat_id": dialog.id,
                "name": getattr(dialog, "name", None),
                "unread_count": unread,
                "messages": [message_row(m) for m in reversed(list(messages))],
            }
        )
        if len(chats) >= max_chats:
            break
    return {"chats": chats, "marks_read": False}


async def get_send_as(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    from telethon.tl import functions

    entity = await client.get_input_entity(_chat(args.get("chat_id")))
    result = await client(functions.channels.GetSendAsRequest(peer=entity))
    peers = []
    for item in getattr(result, "peers", []):
        peer = getattr(item, "peer", None)
        peers.append(
            {
                "peer_id": next((v for k, v in vars(peer).items() if k.endswith("_id")), None)
                if peer is not None
                else None,
                "premium_required": bool(getattr(item, "premium_required", False)),
            }
        )
    return {"send_as": peers}


async def get_member_admin_status(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    entity = await client.get_entity(_chat(args.get("chat_id")))
    user = await client.get_entity(_chat(args.get("user_id")))
    perms = await client.get_permissions(entity, user)
    fields = (
        "is_admin",
        "is_creator",
        "is_banned",
        "has_left",
        "change_info",
        "post_messages",
        "edit_messages",
        "delete_messages",
        "ban_users",
        "invite_users",
        "pin_messages",
        "add_admins",
        "manage_call",
    )
    return {"chat_id": getattr(entity, "id", None), "user_id": getattr(user, "id", None)} | {
        name: bool(getattr(perms, name, False)) for name in fields
    }


async def transcribe_voice(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    from telethon.tl import functions

    entity = await client.get_input_entity(_chat(args.get("chat_id")))
    result = await client(
        functions.messages.TranscribeAudioRequest(
            peer=entity, msg_id=_int(args.get("message_id"), "message_id", 1, 2**31 - 1)
        )
    )
    text = getattr(result, "text", "") or ""
    return {
        "text": text[:8192],
        "pending": bool(getattr(result, "pending", False)),
        "trial_remains": getattr(result, "trial_remains_num", None),
        "untrusted_content": True,
    }


# ---------------------------------------------------------------- writes


async def vote_poll(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    from telethon.tl import functions

    entity = await client.get_input_entity(_chat(args.get("chat_id")))
    message_id = _int(args.get("message_id"), "message_id", 1, 2**31 - 1)
    options = args.get("option_indexes")
    if not isinstance(options, list) or not 1 <= len(options) <= 10:
        raise ProviderError("Telegram option_indexes must list 1-10 answer indexes")
    message = await client.get_messages(entity, ids=message_id)
    poll = getattr(getattr(message, "media", None), "poll", None)
    if poll is None:
        raise ProviderError("Telegram message is not a poll")
    answers = list(poll.answers)
    if getattr(poll, "closed", False):
        raise ProviderError("Telegram poll is closed")
    picked = [_int(i, "option_index", 0, len(answers) - 1) for i in options]
    if len(set(picked)) != len(picked):
        raise ProviderError("Telegram option_indexes must not repeat")
    if len(picked) > 1 and not getattr(poll, "multiple_choice", False):
        raise ProviderError("Telegram poll allows a single answer")
    await client(
        functions.messages.SendVoteRequest(
            peer=entity, msg_id=message_id, options=[answers[i].option for i in picked]
        )
    )
    return {"status": "voted", "message_id": message_id, "option_indexes": picked}


async def remove_user(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    target = _chat(args.get("user_id"))
    if isinstance(target, str) and target.lstrip("@").casefold() in {"me", "self"}:
        # kick_participant(chat, "me") leaves the chat; that is not a removal of a member.
        raise ProviderError("Telegram remove_user cannot remove the account itself")
    entity = await client.get_entity(_chat(args.get("chat_id")))
    user = await client.get_entity(target)
    me = await client.get_me(input_peer=True)
    if getattr(user, "id", None) is not None and getattr(user, "id", None) == getattr(me, "user_id", None):
        raise ProviderError("Telegram remove_user cannot remove the account itself")
    await client.kick_participant(entity, user)
    return {"status": "removed", "chat_id": getattr(entity, "id", None), "user_id": getattr(user, "id", None)}


async def edit_forum_topic(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    from telethon.tl import functions

    entity = await client.get_input_entity(_chat(args.get("chat_id")))
    title = args.get("title")
    if title is not None and (not isinstance(title, str) or not 1 <= len(title.strip()) <= 128):
        raise ProviderError("Telegram title length must be between 1 and 128")
    flags = {k: args[k] for k in ("closed", "hidden") if isinstance(args.get(k), bool)}
    if title is None and not flags:
        raise ProviderError("Telegram edit_forum_topic needs title, closed or hidden")
    topic_id = _int(args.get("topic_id"), "topic_id", 1, 2**31 - 1)
    await client(
        functions.messages.EditForumTopicRequest(peer=entity, topic_id=topic_id, title=title, **flags)
    )
    return {"status": "edited", "topic_id": topic_id}


async def delete_forum_topic(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    from telethon.tl import functions

    entity = await client.get_input_entity(_chat(args.get("chat_id")))
    topic_id = _int(args.get("topic_id"), "topic_id", 2, 2**31 - 1)
    await client(functions.messages.DeleteTopicHistoryRequest(peer=entity, top_msg_id=topic_id))
    return {"status": "deleted", "topic_id": topic_id}


async def rename_folder(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    from telethon.tl import functions, types

    folder_id = _int(args.get("folder_id"), "folder_id", 2, 255)
    title = args.get("title")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 12:
        raise ProviderError("Telegram folder title length must be between 1 and 12")
    result = await client(functions.messages.GetDialogFiltersRequest())
    filters = getattr(result, "filters", result)
    current = next((f for f in filters if isinstance(f, types.DialogFilter) and f.id == folder_id), None)
    if current is None:
        raise ProviderError("Telegram folder not found")
    current.title = types.TextWithEntities(text=title.strip(), entities=[])
    await client(functions.messages.UpdateDialogFilterRequest(id=folder_id, filter=current))
    return {"status": "renamed", "folder_id": folder_id}


NATIVE_READS = {
    "get_message_by_link": get_message_by_link,
    "export_unread_messages": export_unread_messages,
    "get_send_as": get_send_as,
    "get_member_admin_status": get_member_admin_status,
    "transcribe_voice": transcribe_voice,
}
# name -> (handler, risk tier, write category)
NATIVE_WRITES = {
    "vote_poll": (vote_poll, "medium", "stickers_reactions_polls"),
    "remove_user": (remove_user, "high", "groups_admin"),
    "edit_forum_topic": (edit_forum_topic, "medium", "groups_admin"),
    "delete_forum_topic": (delete_forum_topic, "high", "groups_admin"),
    "rename_folder": (rename_folder, "low", "folders_drafts"),
}
