# Changelog

## Unreleased

- Toolsets (`TELEGRAM_TOOLSETS`, `TELEGRAM_TOOLS`, `TELEGRAM_DISABLED_TOOLS`, `--toolsets`), read-only mode (`TELEGRAM_READ_ONLY`, `--read-only`) and write categories for `telegram_write`. Unset = previous surface.
- Per-chat ACL: `TELEGRAM_ALLOWED_CHATS` (deny-by-default when set) and `TELEGRAM_DENIED_CHATS`; listings are filtered.
- Compact tool descriptions and titles; router `instructions`; error envelopes gain `hint` and `docs`; new codes `chat_access_denied`, `tool_disabled`.
- Resources `telegram://toolsets`, `telegram://playbook`, `telegram://guide/{topic}`; prompts `inbox_digest`, `publish_post`, `chat_research`.
- Self-contained skills (`skills/`), Claude Code plugin + marketplace (`.claude-plugin/`), `server.json`, `scripts/token_budget.py` (CI via tests).
- Vendor fixes: GIF search uses the @gif inline bot (Telethon has no `messages.SearchGifsRequest`); `import_contacts` uses `types.InputPhoneContact`.

## 0.4.0

- Telethon 1.44.0 → 1.45.0 (MTProto layer 227 → 229), backward-compatible: `types.User`, `types.Channel`, `types.ChatAdminRights` and `types.ChatBannedRights` only gained optional trailing keyword arguments.
- MCP tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) on every tool, matching the `telegram:read`/`telegram:write` split. Hints only; scopes, bindings and the write allowlist still enforce access.
- `telegram_check_update`: read-only, suggest-only check of the latest GitHub release with release notes link and update command (3 s timeout, 24 h cache, offline result). A cached newer release adds a one-line startup hint; a stale cache is refreshed on a background thread. Opt out with `TELEGRAM_MCP_DISABLE_UPDATE_CHECK=1`.
- The update check goes through the same registrar as the Telegram tools (stdio local scopes, account binding, audit, per-account quota). Only plain version tags (`v1.2.3`) and this repository's release URLs are reported; failed requests back off for 30 minutes and `force=true` is honoured at most once a minute.
- The clean-install gate disables the update check and blocks external DNS in its server process.

## 0.3.0

- Operator-owned media folders, bounded files/albums/voice notes and exclusive bounded downloads to a separate folder; existing account, write policy and durable outbox retained.
- Explicit numeric destination checks for media and polling, with no caller-controlled roots.
- Poll selected chats after acknowledged message IDs; first poll proposes a current baseline. Durable actor/account-bound batches, atomic acknowledgment and safe replay across restart.
- Installable setup wizard and JSON/TOML client snippets that work outside the checkout.
- English and Russian agency information, integration contact and package installation guide.

## 0.2.0

- Explicit config-file startup and offline configuration doctor.
- Install/client guide, local setup wizard and clean-wheel validation.
- Recipient search marks incomplete results as ambiguous. Messages remain untrusted content. Media and event-feed expansion are future work.
- LicenseRef-ZAI-ONE and Issues-based feedback policy.
