"""Static toolsets, read-only mode and per-chat access control.

Filtering happens once at registration (no dynamic meta-tools). Env:

* ``TELEGRAM_TOOLSETS`` - ``all`` (default, backward compatible), ``default``
  or a comma list of toolset names.
* ``TELEGRAM_TOOLS`` - extra tool names added on top of the toolsets.
* ``TELEGRAM_DISABLED_TOOLS`` - tool names removed last.
* ``TELEGRAM_READ_ONLY=1`` - drop every write tool and write category.
* ``TELEGRAM_ALLOWED_CHATS`` / ``TELEGRAM_DENIED_CHATS`` - comma lists of
  numeric chat ids or ``@usernames``. Unset allowlist = every chat allowed
  (backward compatible); a set allowlist denies every other chat.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from zai_telegram.adapter import TELEGRAM_WRITE_ALLOWLIST

# Platform toolsets -> exposed MCP tools.
TOOLSETS: dict[str, dict[str, Any]] = {
    "core": {
        "description": "Chats, inbox, chat metadata, recipient resolution, update check.",
        "tools": (
            "telegram_list_chats",
            "telegram_get_inbox",
            "telegram_get_chat",
            "telegram_resolve_recipient",
            "telegram_check_update",
        ),
    },
    "messages_read": {
        "description": "Read history, search messages, conversation context.",
        "tools": ("telegram_get_messages", "telegram_search_messages", "telegram_get_conversation_context"),
    },
    "messages_write": {
        "description": "Send/reply, batch send, delivery ledger (needs TELEGRAM_WRITE_ENABLED).",
        "tools": (
            "telegram_send_message",
            "telegram_reply_to_message",
            "telegram_send_many",
            "telegram_outbox_status",
        ),
    },
    "events": {
        "description": "Cursor-based polling of selected chats without read receipts.",
        "tools": ("telegram_poll_messages", "telegram_acknowledge_poll"),
    },
    "advanced_write": {
        "description": "Generic telegram_write over the reviewed allowlist, limited to enabled write categories.",
        "tools": ("telegram_write",),
    },
}

# Upstream write tool -> write category (gates the generic ``telegram_write``).
WRITE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "messages_write": (
        "send_message",
        "send_scheduled_message",
        "delete_scheduled_message",
        "forward_message",
        "forward_messages",
        "edit_message",
        "delete_message",
        "delete_chat_history",
        "delete_messages_bulk",
        "pin_message",
        "unpin_message",
        "unpin_all_messages",
        "mark_as_read",
        "reply_to_message",
    ),
    "stickers_reactions_polls": (
        "create_poll",
        "send_reaction",
        "remove_reaction",
        "press_inline_button",
        "send_sticker",
        "send_gif",
    ),
    "folders_drafts": (
        "save_draft",
        "clear_draft",
        "create_folder",
        "add_chat_to_folder",
        "remove_chat_from_folder",
        "delete_folder",
        "reorder_folders",
        "mute_chat",
        "unmute_chat",
        "archive_chat",
        "unarchive_chat",
    ),
    "contacts": (
        "add_contact",
        "delete_contact",
        "block_user",
        "unblock_user",
        "import_contacts",
        "send_contact",
    ),
    "groups_admin": (
        "create_group",
        "invite_to_group",
        "leave_chat",
        "edit_chat_title",
        "edit_chat_photo",
        "edit_chat_about",
        "delete_chat_photo",
        "promote_admin",
        "demote_admin",
        "ban_user",
        "unban_user",
        "set_default_chat_permissions",
        "toggle_slow_mode",
        "edit_admin_rights",
        "join_chat_by_link",
        "import_chat_invite",
        "enable_forum_topics",
        "create_forum_topic",
    ),
    "channels": ("create_channel", "subscribe_public_channel"),
    "media": ("send_file", "send_album", "download_media", "send_voice", "upload_file"),
    "profile_privacy": (
        "update_profile",
        "set_profile_photo",
        "delete_profile_photo",
        "set_privacy_settings",
        "set_bot_commands",
    ),
}

DEFAULT_TOOLSETS = ("core", "messages_read", "messages_write", "events")
WRITE_TOOLSETS = frozenset({"messages_write", "advanced_write"} | set(WRITE_CATEGORIES))
ALL_TOOLSETS = tuple(TOOLSETS) + tuple(name for name in WRITE_CATEGORIES if name not in TOOLSETS)
ALL_TOOLS = frozenset(tool for spec in TOOLSETS.values() for tool in spec["tools"])
WRITE_TOOL_NAMES = frozenset(TOOLSETS["messages_write"]["tools"] + TOOLSETS["advanced_write"]["tools"])

# Argument names that address a chat; checked against the chat ACL.
CHAT_ARGUMENTS = ("chat_id", "to_chat_id", "from_chat_id", "group_id", "channel_id", "peer")


def _split(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _norm_chat(value: Any) -> str:
    text = str(value).strip()
    if text.startswith("telegram:"):
        text = text[len("telegram:") :]
    return text.lstrip("@").casefold() if not text.lstrip("-").isdigit() else text


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ChatAccess:
    allowed: frozenset[str] | None = None  # None = all chats allowed
    denied: frozenset[str] = frozenset()

    @property
    def active(self) -> bool:
        return self.allowed is not None or bool(self.denied)

    def permits(self, chat: Any) -> bool:
        key = _norm_chat(chat)
        if key in self.denied:
            return False
        return self.allowed is None or key in self.allowed

    def permits_record(self, record: Mapping[str, Any]) -> bool:
        primary = record.get("chat_id", record.get("id"))
        keys = [key for key in (primary, record.get("username")) if key not in (None, "")]
        if any(_norm_chat(key) in self.denied for key in keys):
            return False
        if self.allowed is None:
            return True
        # Allowlist: one matching identity is enough; records without identity fail closed.
        return any(_norm_chat(key) in self.allowed for key in keys)


@dataclass(frozen=True)
class Surface:
    toolsets: tuple[str, ...] = ALL_TOOLSETS
    tools: frozenset[str] = ALL_TOOLS
    write_tools: frozenset[str] = frozenset(TELEGRAM_WRITE_ALLOWLIST)
    read_only: bool = False
    chats: ChatAccess = field(default_factory=ChatAccess)

    def exposes(self, tool: str) -> bool:
        return tool in self.tools

    def describe(self) -> dict[str, Any]:
        return {
            "active": list(self.toolsets),
            "read_only": self.read_only,
            "chat_acl": {
                "mode": "allowlist"
                if self.chats.allowed is not None
                else ("denylist" if self.chats.denied else "off"),
                "allowed_count": len(self.chats.allowed or ()),
                "denied_count": len(self.chats.denied),
            },
            "toolsets": {
                name: {
                    "description": spec["description"],
                    "tools": list(spec["tools"]),
                    "enabled": name in self.toolsets,
                }
                for name, spec in TOOLSETS.items()
            },
            "write_categories": {
                name: {"upstream_tools": list(tools), "enabled": name in self.toolsets and not self.read_only}
                for name, tools in WRITE_CATEGORIES.items()
            },
            "env": {
                "TELEGRAM_TOOLSETS": "all | default | comma list",
                "TELEGRAM_TOOLS": "extra tool names",
                "TELEGRAM_DISABLED_TOOLS": "tool names to hide",
                "TELEGRAM_READ_ONLY": "1 hides every write",
                "TELEGRAM_ALLOWED_CHATS": "ids/@usernames; set = deny others",
                "TELEGRAM_DENIED_CHATS": "ids/@usernames always denied",
            },
        }

    def describe_json(self) -> str:
        return json.dumps(self.describe(), ensure_ascii=False, separators=(",", ":"))


def build_surface(
    toolsets: str | Iterable[str] | None = None,
    *,
    extra_tools: Iterable[str] = (),
    disabled_tools: Iterable[str] = (),
    read_only: bool = False,
    allowed_chats: Iterable[str] | None = None,
    denied_chats: Iterable[str] = (),
) -> Surface:
    if toolsets is None or toolsets == "all" or (isinstance(toolsets, str) and not toolsets.strip()):
        names: list[str] = list(ALL_TOOLSETS)
    else:
        raw = _split(toolsets) if isinstance(toolsets, str) else list(toolsets)
        names = []
        for name in raw:
            expanded = (
                list(DEFAULT_TOOLSETS)
                if name == "default"
                else list(ALL_TOOLSETS)
                if name == "all"
                else [name]
            )
            for item in expanded:
                if item not in ALL_TOOLSETS:
                    raise ValueError(f"unknown Telegram toolset: {item}")
                if item not in names:
                    names.append(item)
    if read_only:
        names = [name for name in names if name not in WRITE_TOOLSETS]
    tools = {tool for name in names if name in TOOLSETS for tool in TOOLSETS[name]["tools"]}
    # A write category alone implies the generic tool that carries it.
    if any(name in WRITE_CATEGORIES and name != "messages_write" for name in names):
        tools.add("telegram_write")
    for tool in extra_tools:
        if tool not in ALL_TOOLS:
            raise ValueError(f"unknown Telegram tool: {tool}")
        tools.add(tool)
    tools -= set(disabled_tools)
    if read_only:
        tools -= WRITE_TOOL_NAMES
    write_tools = (
        frozenset()
        if read_only
        else frozenset(tool for name in names if name in WRITE_CATEGORIES for tool in WRITE_CATEGORIES[name])
    )
    allowed = None if allowed_chats is None else frozenset(_norm_chat(item) for item in allowed_chats)
    return Surface(
        toolsets=tuple(names),
        tools=frozenset(tools),
        write_tools=write_tools,
        read_only=read_only,
        chats=ChatAccess(allowed=allowed, denied=frozenset(_norm_chat(item) for item in denied_chats)),
    )


def surface_from_env(
    environ: Mapping[str, str] | None = None, *, cli_toolsets: str | None = None, cli_read_only: bool = False
) -> Surface:
    env = os.environ if environ is None else environ
    allowed_raw = env.get("TELEGRAM_ALLOWED_CHATS")
    return build_surface(
        cli_toolsets if cli_toolsets is not None else env.get("TELEGRAM_TOOLSETS"),
        extra_tools=_split(env.get("TELEGRAM_TOOLS")),
        disabled_tools=_split(env.get("TELEGRAM_DISABLED_TOOLS")),
        read_only=cli_read_only or _truthy(env.get("TELEGRAM_READ_ONLY")),
        allowed_chats=_split(allowed_raw) if allowed_raw is not None and allowed_raw.strip() else None,
        denied_chats=_split(env.get("TELEGRAM_DENIED_CHATS")),
    )


def chat_targets(tool: str, arguments: Mapping[str, Any]) -> list[Any]:
    """Every chat a call addresses, including nested generic-write and batch items."""
    targets: list[Any] = []
    for name in CHAT_ARGUMENTS:
        if arguments.get(name) is not None and tool != "telegram_resolve_recipient":
            targets.append(arguments[name])
    for value in arguments.get("chat_ids") or ():
        targets.append(value)
    nested = arguments.get("arguments")
    if tool == "telegram_write" and isinstance(nested, Mapping):
        targets.extend(chat_targets("upstream", nested))
    for item in arguments.get("items") or ():
        if isinstance(item, Mapping) and item.get("recipient_ref") is not None:
            targets.append(item["recipient_ref"])
    return targets


_RESULT_LISTS = ("results", "chats", "candidates")
# Tools whose results list many chats and are filtered by the ACL.
LISTING_TOOLS = frozenset(
    {"telegram_list_chats", "telegram_get_inbox", "telegram_search_messages", "telegram_resolve_recipient"}
)


def filter_result(value: Any, chats: ChatAccess) -> Any:
    """Drop records for chats outside the ACL from list-shaped results."""
    if not chats.active:
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        filtered = filter_result(decoded, chats)
        return value if filtered == decoded else json.dumps(filtered, ensure_ascii=False)
    if isinstance(value, list):
        return [item for item in value if not isinstance(item, Mapping) or chats.permits_record(item)]
    if isinstance(value, dict):
        out = dict(value)
        for key in _RESULT_LISTS:
            if isinstance(out.get(key), list):
                out[key] = filter_result(out[key], chats)
        if isinstance(out.get("recipient"), Mapping) and not chats.permits_record(out["recipient"]):
            out["recipient"], out["status"] = None, "not_found"
        return out
    return value
