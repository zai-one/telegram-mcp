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

import contextlib
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
            "telegram_list_accounts",
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
    "chat_insights": {
        "description": "Forum topics, admins, channel/supergroup statistics.",
        "tools": ("telegram_list_topics", "telegram_get_admins", "telegram_get_chat_stats"),
    },
    "events": {
        "description": "Cursor polling and bounded waits (new / settled debounce), no read receipts.",
        "tools": (
            "telegram_poll_messages",
            "telegram_acknowledge_poll",
            "telegram_wait_for_new_message",
            "telegram_wait_for_settled_message",
        ),
    },
    "advanced_read": {
        "description": "Opt-in: generic telegram_read over ~50 reviewed Telethon reads (participants, pinned, reactions, folders, profiles...).",
        "tools": ("telegram_read",),
    },
    "event_feed": {
        "description": "Opt-in: live feed of new/edited/deleted messages (Telethon events, cursor poll).",
        "tools": ("telegram_poll_events",),
    },
    "account_admin": {
        "description": "Opt-in, stdio only: add an account in chat via QR login (no codes or passwords in chat).",
        "tools": ("telegram_add_account",),
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
        "vote_poll",
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
        "rename_folder",
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
        "edit_forum_topic",
        "delete_forum_topic",
        "remove_user",
        "get_invite_link",
        "export_chat_invite",
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

DEFAULT_TOOLSETS = ("core", "messages_read", "messages_write", "events", "chat_insights")
WRITE_TOOLSETS = frozenset({"messages_write", "advanced_write", "account_admin"} | set(WRITE_CATEGORIES))
# Opt-in toolsets are never part of "all": name them explicitly (token budget).
OPT_IN_TOOLSETS = frozenset({"advanced_read", "event_feed", "account_admin"})
KNOWN_TOOLSETS = tuple(TOOLSETS) + tuple(name for name in WRITE_CATEGORIES if name not in TOOLSETS)
ALL_TOOLSETS = tuple(name for name in KNOWN_TOOLSETS if name not in OPT_IN_TOOLSETS)
KNOWN_TOOLS = frozenset(tool for spec in TOOLSETS.values() for tool in spec["tools"])
ALL_TOOLS = frozenset(
    tool for name, spec in TOOLSETS.items() if name not in OPT_IN_TOOLSETS for tool in spec["tools"]
)
WRITE_TOOL_NAMES = frozenset(TOOLSETS["messages_write"]["tools"] + TOOLSETS["advanced_write"]["tools"])

# Argument names that address a chat; checked against the chat ACL.
CHAT_ARGUMENTS = ("chat_id", "to_chat_id", "from_chat_id", "group_id", "channel_id", "peer")
# Upstream write arguments that address a person or a public channel. When a
# write names no chat, these are its target (the private chat / the channel);
# when it names a chat, the users are members of that chat and the chat governs.
WRITE_PEER_ARGUMENTS = ("user_id", "user_ids", "channel")
# Upstream writes that act on the account or create/join chats outside any
# allowlist entry (new group/channel, invite links, profile, privacy, folders
# spanning chat types, bot commands, contacts import). With an allowlist set
# they are denied even when an argument happens to match an allowed chat.
ACCOUNT_WIDE_WRITES = frozenset(
    {
        "create_group",
        "create_channel",
        "join_chat_by_link",
        "import_chat_invite",
        "create_folder",
        "delete_folder",
        "reorder_folders",
        "add_contact",
        "import_contacts",
        "update_profile",
        "set_profile_photo",
        "delete_profile_photo",
        "set_privacy_settings",
        "set_bot_commands",
        "upload_file",
        "rename_folder",
    }
)
# Upstream reads that name one user but return that user's other chats. Under
# any chat ACL they would list chats outside it, so they are denied.
CHAT_LISTING_READS = frozenset({"get_common_chats", "get_contact_chats", "export_unread_messages"})


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
    # "full" (default, unchanged payloads) or "summary" (compact; see compact()).
    detail: str = "full"
    max_text_chars: int = 500

    def exposes(self, tool: str) -> bool:
        return tool in self.tools

    def describe(self) -> dict[str, Any]:
        return {
            "active": list(self.toolsets),
            "read_only": self.read_only,
            "detail": self.detail,
            "max_text_chars": self.max_text_chars,
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
                "TELEGRAM_TOOL_LOADING": "eager (default) | lazy: core tools + telegram_enable_toolsets",
                "TELEGRAM_ALLOWED_CHATS": "ids/@usernames; set = deny others",
                "TELEGRAM_DENIED_CHATS": "ids/@usernames always denied",
                "TELEGRAM_RESPONSE_DETAIL": "full (default) | summary: drop empty fields, truncate long texts",
                "TELEGRAM_MAX_TEXT_CHARS": "summary mode text limit (default 500)",
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
    detail: str = "full",
    max_text_chars: int = 500,
) -> Surface:
    if detail not in {"full", "summary"}:
        raise ValueError("TELEGRAM_RESPONSE_DETAIL must be full or summary")
    if not 50 <= max_text_chars <= 4096:
        raise ValueError("TELEGRAM_MAX_TEXT_CHARS must be 50..4096")
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
                if item not in KNOWN_TOOLSETS:
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
        if tool not in KNOWN_TOOLS:
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
        detail=detail,
        max_text_chars=max_text_chars,
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
        detail=(env.get("TELEGRAM_RESPONSE_DETAIL") or "full").strip().lower(),
        max_text_chars=int(env.get("TELEGRAM_MAX_TEXT_CHARS") or 500),
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
        named = chat_targets("upstream", nested)
        if not named:
            for name in WRITE_PEER_ARGUMENTS:
                value = nested.get(name)
                if isinstance(value, list | tuple):
                    named.extend(item for item in value if item is not None)
                elif value is not None and value != "":
                    named.append(value)
        targets.extend(named)
    if tool == "telegram_read" and isinstance(nested, Mapping):
        targets.extend(chat_targets("upstream", nested))
        # A user/contact id addresses that person's private chat.
        targets.extend(nested[key] for key in ("user_id", "contact_id", "username") if nested.get(key))
        if nested.get("link") is not None:
            # A t.me message link names its chat: the ACL checks exactly the chat the
            # native reader will open. An unparsable link adds no target (fail closed).
            from zai_telegram.native import parse_message_link
            from zai_telegram.transport import ProviderError

            with contextlib.suppress(ProviderError):
                targets.append(parse_message_link(nested["link"])[0])
    for item in arguments.get("items") or ():
        if isinstance(item, Mapping) and item.get("recipient_ref") is not None:
            targets.append(item["recipient_ref"])
    return targets


def read_denied_by_acl(chats: ChatAccess, arguments: Mapping[str, Any]) -> bool:
    """Fail-closed rule for the generic ``telegram_read`` under a chat ACL.

    Account-wide reads (contacts, drafts, global search) name no chat, and
    chat-listing reads (common chats of a user) return chats outside the ACL.
    """
    if not chats.active:
        return False
    return arguments.get("tool") in CHAT_LISTING_READS or not chat_targets("telegram_read", arguments)


def write_denied_by_allowlist(chats: ChatAccess, arguments: Mapping[str, Any]) -> bool:
    """Fail-closed rule for the generic ``telegram_write`` under an allowlist.

    A write whose target cannot be matched to an allowed chat (no chat, user or
    channel named, or an account-wide/chat-creating tool) is denied.
    """
    if chats.allowed is None:
        return False
    return arguments.get("tool") in ACCOUNT_WIDE_WRITES or not chat_targets("telegram_write", arguments)


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


# Fields that carry message text; truncated in summary mode.
TEXT_FIELDS = frozenset({"message", "text", "about", "caption"})
# Keys dropped in summary mode (raw/duplicated structures).
NOISE_FIELDS = frozenset({"raw", "entities", "media_raw"})


def compact(value: Any, max_text_chars: int) -> Any:
    """Summary mode: drop null/empty/noise fields, cut long texts and mark them."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        if isinstance(decoded, dict | list):
            return json.dumps(compact(decoded, max_text_chars), ensure_ascii=False, separators=(",", ":"))
        return value
    if isinstance(value, list):
        return [compact(item, max_text_chars) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in NOISE_FIELDS or item is None or item == "" or item == [] or item == {}:
                continue
            if key in TEXT_FIELDS and isinstance(item, str) and len(item) > max_text_chars:
                out[key] = item[:max_text_chars] + "…"
                out[f"{key}_truncated"] = True
                out[f"{key}_length"] = len(item)
                continue
            out[key] = compact(item, max_text_chars)
        return out
    return value


@dataclass(frozen=True)
class LazyProfile:
    """What lazy_loading.install needs: toolsets, one-line notes and the start core."""

    service: str = "telegram"
    env: str = "TELEGRAM"
    lazy_core: tuple[str, ...] = (
        *TOOLSETS["core"]["tools"],
        "telegram_get_conversation_context",
        "telegram_search_messages",
        "telegram_send_message",
    )

    @property
    def toolsets(self) -> dict[str, tuple[str, ...]]:
        return {name: tuple(spec["tools"]) for name, spec in TOOLSETS.items()}

    @property
    def toolset_notes(self) -> dict[str, str]:
        return {name: spec["description"] for name, spec in TOOLSETS.items()}


LAZY_PROFILE = LazyProfile()

