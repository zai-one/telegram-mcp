# Changelog

## Unreleased

- Telethon 1.44.0 → 1.45.0 (MTProto layer 227 → 229). The change is backward-compatible for the embedded runtime: `types.User`, `types.Channel`, `types.ChatAdminRights` and `types.ChatBannedRights` gained optional trailing keyword arguments; existing calls are unaffected.
- MCP tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) on every tool, matching the existing `telegram:read`/`telegram:write` split. Hints only; scopes, bindings and the write allowlist still enforce access.
- `telegram_check_update`: read-only, suggest-only check of the latest GitHub release (3 s timeout, 24 h cache, graceful offline result) with release notes link and update command. A cached newer release adds a one-line hint to the startup MCP instructions; the stale cache is refreshed on a background thread. Opt out with `TELEGRAM_MCP_DISABLE_UPDATE_CHECK=1`.
- Update check hardening: only plain version tags (`v1.2.3`) and URLs of this repository's releases are reported, from GitHub and from the cache; anything else is treated as no release. Every attempt is cached: a failed request is not repeated for 30 minutes and `force=true` is honoured at most once a minute; the startup refresh obeys the same rules.
- `telegram_check_update` is now registered through the same registrar as the Telegram tools: stdio local scopes, account binding, audit and the per-account quota apply. Name and input schema are unchanged.
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
