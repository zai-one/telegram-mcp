🇬🇧 English · [🇷🇺 Русский](README.ru.md)

# Telegram MCP

<!-- mcp-name: io.github.zai-one/telegram-mcp -->

**Catch up on a conversation and prepare your reply in the same chat.**

> MCP server for your own Telegram account (MTProto): read, search and summarise chats, channels and groups; send only when allowed, with per-chat ACL and idempotent sends.

Find an earlier agreement, collect context from a busy conversation or review incoming messages before replying. Telegram MCP connects your assistant to a Telegram user account. It starts with read-only access; sending can be enabled with separate permissions.

[Quick start](#quick-start) · [Connect your assistant](#connect-your-assistant) · [Issues](https://github.com/zai-one/telegram-mcp/issues)

Try asking your assistant:

> Read the recent messages in the chat I select. Summarise the agreements and open questions, then draft a reply for me to review.

## What you can do

| Your task | What the MCP server provides |
|---|---|
| Catch up | List chats, read recent messages and retrieve conversation context for the assistant to summarise. |
| Find the right detail | Search messages and resolve a recipient before writing. |
| Send and follow up | Send or reply when permitted, submit message batches and inspect outbox status. |
| Work with attachments | [Files, albums and voice notes](docs/MEDIA_AND_POLLING.md) from operator-approved folders, plus bounded downloads to a separate folder. |
| Keep up with selected chats | Poll new message IDs and acknowledge processed batches. Cursors survive a restart; unacknowledged messages can be read again. |

## Why this MCP

- Optional lazy tool loading: set `TELEGRAM_TOOL_LOADING=lazy` (or choose it in the setup wizard) to start with a small core and enable toolsets on demand; see [Lazy tool loading](#lazy-tool-loading).
- Built on the [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp) v3.2.0 tool set (Apache-2.0, vendored with provenance) and adds safety on top.
- Starts read-only; toolsets, per-chat allow/deny ACL, explicit-confirmation sends with idempotency keys and an outbox.
- Message text is treated as untrusted; reading never marks messages as read; structured errors with `retry_after_seconds` for flood waits.
- Compact replies, conversation context and chat insights (topics, admins, channel stats); polling cursors survive restarts.

## Lazy tool loading

Optional lazy tool loading: `TELEGRAM_TOOL_LOADING=lazy` starts with a small core plus `telegram_enable_toolsets` and adds toolsets on demand (`tools/list_changed`). Startup cost (tools/list + instructions, ≈chars/4): eager ≈3558 tokens, lazy ≈1817 tokens (−49%). Default `eager` is unchanged; if your client ignores `tools/list_changed`, keep `eager` or reconnect after enabling. Scope: enablement is per MCP session (FastMCP session visibility) for handshake-era clients (initialize), so one client's enabled toolsets never appear for another. Sessionless clients (2026-07-28 `server/discover`) and the stateless HTTP transport get a new session per request, so enablement does not persist there: use `eager` or `TELEGRAM_TOOLSETS`.

Clients and `notifications/tools/list_changed` (verified against official docs, changelogs or maintainer statements, 2026-10):

| Client | `tools/list_changed` | Source |
|---|---|---|
| Claude Code | Supported (interactive: tools, prompts, resources; `-p`/Agent SDK: tools) | [docs: Dynamic tool updates](https://code.claude.com/docs/en/mcp) |
| VS Code (GitHub Copilot) | Supported (dynamic tool discovery); new tools may reach the agent only on the next turn | [docs](https://code.visualstudio.com/api/extension-guides/ai/mcp), [#303012](https://github.com/microsoft/vscode/issues/303012) |
| Gemini CLI | Supported (refreshes the tool registry) | [PR #14375](https://github.com/google-gemini/gemini-cli/pull/14375), [PR #21050](https://github.com/google-gemini/gemini-cli/pull/21050) |
| Cursor | Unreliable: staff confirm a regression in 3.12.x; restart Cursor after enabling | [forum](https://forum.cursor.com/t/mcp-server-regression-does-not-reload-tools-disconnect-does-nothing-ignores-mcp-json-changes-etc/166216) |
| Claude Desktop, Windsurf, Codex CLI, Cline, Zed, Continue | Unverified: no official statement found; use `eager` or reconnect after enabling | — |

## Maintained and up to date

- Actively maintained by ZAI.ONE: current version 0.6.0; release notes in [CHANGELOG.md](CHANGELOG.md) and [Releases](https://github.com/zai-one/telegram-mcp/releases).
- Built-in update check: ask the assistant to run `telegram_check_update` (public GitHub API, no credentials, cached; nothing is installed automatically; `TELEGRAM_MCP_DISABLE_UPDATE_CHECK=1` turns it off).
- Follows current MCP practice: tool annotations (read-only / destructive hints), toolsets and read-only mode, server `instructions`, prompts and resources, structured errors with recovery hints, secrets kept out of tool output, a token budget for tool descriptions enforced in tests.
- CI runs the test suite on Linux and Windows for every pull request.

## Quick start

### One minute with uvx

Needs only [uv](https://docs.astral.sh/uv/getting-started/installation/) and an API ID/hash from [my.telegram.org](https://my.telegram.org/apps).

```sh
uvx --from git+https://github.com/zai-one/telegram-mcp@v0.6.0 zai-telegram-mcp-setup --directory ~/telegram-mcp
```

The wizard logs you in interactively, stores secrets in private files and writes `mcp-client.json` (a ready `uvx` entry) into that directory. Paste its `mcpServers` block into Claude Desktop / Cursor, or for Claude Code:

```sh
claude mcp add telegram -- uvx --from git+https://github.com/zai-one/telegram-mcp@v0.6.0 zai-telegram-mcp --config ~/telegram-mcp/mcp.local.json
```

Then ask: *"What's new in my Telegram? Don't mark anything read."* An npm/`npx` shim is not provided: it would still need Python/uv, so uvx is the shortest path.

### From a clone

Prefer a ready package? [Install the release and generate your client configuration](INSTALL.md#install-a-release-package). No source checkout is required.

Install **Python 3.12–3.14** and [uv](https://docs.astral.sh/uv/getting-started/installation/). Clone with Git or [download the ZIP](https://github.com/zai-one/telegram-mcp/archive/refs/heads/main.zip). With a ZIP, open the extracted directory and skip the first two commands.

You need a Telegram API ID, API hash and an authenticated user session. The local wizard can create the session through an interactive login. A bot token cannot replace the user session. See [Telegram login setup](INSTALL.md#from-a-clone-or-source-zip).

```sh
git clone https://github.com/zai-one/telegram-mcp.git
cd telegram-mcp
uv sync --frozen
uv run --frozen python scripts/configure.py
uv run --frozen zai-telegram-mcp --config mcp.local.json --check-config
```

The wizard creates a local configuration and stores secrets in private files. It refuses to overwrite an existing setup. `--check-config` validates local settings; the first request below checks your account connection.

## Connect your assistant

Add this configuration to an MCP client that uses `mcpServers`, such as Claude Desktop or Cursor. Replace `/ABSOLUTE/PATH/` with your absolute path; Windows JSON paths can use forward slashes, such as `D:/Tools/`.

```json
{
  "mcpServers": {
    "telegram": {
      "command": "uv",
      "args": [
        "--directory",
        "/ABSOLUTE/PATH/telegram-mcp",
        "run",
        "--frozen",
        "zai-telegram-mcp",
        "--config",
        "/ABSOLUTE/PATH/telegram-mcp/mcp.local.json"
      ]
    }
  }
}
```

The client starts the MCP server for you. Refresh its tool list, then make your first request. For clients with a different config format, reuse the same `command` and `args`; `uv` must be available to the client process.

### First request

> Show my recent Telegram chats. Ask which one to open before reading its messages.

A valid user session returns a list of chats. Choose one to retrieve messages or conversation context. Drafting and summarising happen in your AI assistant; the MCP server supplies Telegram data and supported actions.

If tools do not appear, check the absolute path, whether the client can find `uv`, and the `--check-config` result. For access errors, check account credentials and permissions. [Installation and troubleshooting](INSTALL.md).

### Other clients

```bash
# Claude Code (MCP only)
claude mcp add telegram -- uvx --from git+https://github.com/zai-one/telegram-mcp@v0.6.0 zai-telegram-mcp --config /ABSOLUTE/PATH/mcp.local.json
# Claude Code plugin (MCP + skills) from this repository; set TELEGRAM_MCP_CONFIG to your mcp.local.json
/plugin marketplace add zai-one/telegram-mcp
/plugin install telegram@zai-telegram
```

Codex `~/.codex/config.toml`:

```toml
[mcp_servers.telegram]
command = "uvx"
args = ["--from", "git+https://github.com/zai-one/telegram-mcp@v0.6.0", "zai-telegram-mcp", "--config", "/ABSOLUTE/PATH/mcp.local.json"]
env = { TELEGRAM_TOOLSETS = "default" }
```

Cursor `.cursor/mcp.json` and Claude Desktop `claude_desktop_config.json` use the `mcpServers` block above (or `"command": "uvx"` with the same args as Codex).

## Toolsets, read-only mode and chat access

Nothing changes unless you set these variables (default: every tool, every chat).

| Variable | Effect |
|---|---|
| `TELEGRAM_TOOLSETS` | `all` (default), `default` (= core, messages_read, messages_write, events) or a comma list. Also `--toolsets`. |
| `TELEGRAM_TOOLS` / `TELEGRAM_DISABLED_TOOLS` | Add or hide individual tools. |
| `TELEGRAM_READ_ONLY=1` | Hide every write tool (also `--read-only`). |
| `TELEGRAM_ALLOWED_CHATS` | Comma list of chat ids / @usernames. When set, every other chat is denied and hidden from listings, and calls that name no allowed chat (account-wide writes such as joining by link, creating groups, profile or privacy changes) are denied. |
| `TELEGRAM_DENIED_CHATS` | Always denied. |
| `TELEGRAM_RESPONSE_DETAIL` | `full` (default, unchanged) or `summary`: drops empty fields and cuts message texts to `TELEGRAM_MAX_TEXT_CHARS` (default 500) with `message_truncated`/`message_length`. |

Toolsets: `core`, `messages_read`, `messages_write`, `events`, `chat_insights` (`telegram_list_topics`, `telegram_get_admins`, `telegram_get_chat_stats`), opt-in `event_feed` (`telegram_poll_events`), opt-in `account_admin` (`telegram_add_account`), opt-in `advanced_read` (generic `telegram_read` over ~50 reviewed Telethon reads: participants, pinned, reactions, read-by, folders, drafts, profiles, message by t.me link, unread export, member rights, voice transcription; never part of `all`, enable with `TELEGRAM_TOOLSETS=all,advanced_read`; list in `telegram://guide/advanced-read`), `advanced_write` (generic `telegram_write`), and write categories for `telegram_write`: `media`, `contacts`, `groups_admin`, `channels`, `folders_drafts`, `profile_privacy`, `stickers_reactions_polls`. Writes still need `TELEGRAM_WRITE_ENABLED=true` and the `telegram:write` scope. The assistant can read the live setup from `telegram://toolsets`. Errors are JSON with `code`, `retryable`, `hint` and `docs` (`telegram://guide/errors`).

### Several accounts and waiting for replies

- **Multi-account:** add `TELEGRAM_SESSION_STRING_<LABEL>` (e.g. `_WORK`) to the secret file and bind a list in the bindings file: `{"local-operator": ["default", "work"]}`. The first label is the default; every tool then accepts an optional `account` argument and `telegram_list_accounts` shows the labels. Rate limits, cursors and the outbox are per account (outbox records show `account_label`); acknowledge a poll or wait batch with the same `account` that produced it. A plain string binding keeps the old single-account schema (no extra tokens).
- **Wait for a message:** `telegram_wait_for_new_message(chat_ids, timeout_seconds<=55)` blocks until a new message arrives or returns `status=timeout`. **Settled burst:** `telegram_wait_for_settled_message(chat_ids, settle_seconds=6, max_wait_seconds<=55)` waits until the chat goes quiet (`settled` / `partial` / `timeout`). Both respect the chat allowlist, never send read receipts and pace reads (≤ ~24/min).
- **Live event feed (opt-in `event_feed`):** `telegram_poll_events` subscribes to Telethon update events and returns **new, edited and deleted** messages across all chats after a client-held cursor (`wait_seconds<=55`, filters `chat_ids`/`kinds`, `gap=true` after a restart or overflow). One feed per account, filtered by the chat ACL. Across restarts only ids and dates are kept (24 h, never text; `TELEGRAM_FEED_PERSIST=0` disables), so a cursor stays valid, messages sent during the downtime are backfilled as `new` events (`TELEGRAM_FEED_BACKFILL=0` disables), and the downtime is reported (`gap_window`). Guide `telegram://guide/events`.
- **Add an account in chat (opt-in `account_admin`, local stdio only):** `telegram_add_account(label, action=start|status|cancel)` shows a QR code to scan in Telegram (Settings → Devices → Link Desktop Device). No phone, code or 2FA password ever passes through the chat; 2FA accounts finish in a terminal. The session goes straight to the private secret file (never overwritten, never shown). Like every write it needs `TELEGRAM_WRITE_ENABLED=true` and is hidden in read-only mode. The new account is usually active immediately (hot-activated, `restart_required=false`; every tool takes `account` while `account_admin` is on); restart only if the result says `restart_required=true`.
- **Add an account from a terminal:** `zai-telegram-mcp-setup --add-account work --directory <setup dir> [--qr]` logs in by phone code or QR (2FA password typed hidden), appends `TELEGRAM_SESSION_STRING_WORK` to the existing secret file and grants `work` in the bindings file. A running server picks it up on its next call (no restart) and announces the changed tool list.

## Skills, prompts and guides

Self-contained skills in [`skills/`](skills/) (each folder has `SKILL.md` plus its own `references/`, `templates/`, `scripts/`): `telegram-inbox-triage`, `telegram-safe-send`, `telegram-chat-research`, `telegram-group-admin`, `telegram-channel-report`. Copy a folder into your client's skills directory (`~/.claude/skills/`, `.agents/skills/`, `.cursor/skills/`) or install the plugin. Clients without skills get MCP prompts `inbox_digest`, `publish_post`, `chat_research` and resources `telegram://playbook`, `telegram://guide/{formatting|errors|flood-wait|acl|search-syntax|permissions}`.

### Example tasks

- "Triage my unread Telegram chats; nothing should be marked read."
- "Find what the team said about the invoice last week."
- "Draft a post for @my_channel about the release, show it to me, then publish."
- "Reply to Ivan's last message: we'll ship on Friday."
- "Watch chat -100123 and tell me when someone asks a question."
- "Ban user 123 from our group after I confirm."

## Access and limits

Your session grants access to Telegram data, so use a client you trust. The assistant receives messages you request. Incomplete recipient searches remain ambiguous. [Media setup and message polling](docs/MEDIA_AND_POLLING.md) explain file permissions, size limits and acknowledgment. Polling tracks new message IDs; it does not track edits or deletions and is not a live event stream.

Authenticated HTTP is available for a server deployment. See [HTTP setup](INSTALL.md#http), [configuration and permissions](docs/RUNTIME.md) and [Python package integration](INSTALL.md#python-package-and-platform-integration).

### Update notifications

`telegram_check_update` compares the installed version with the [latest GitHub release](https://github.com/zai-one/telegram-mcp/releases) and returns the release notes link plus the command to update (a release wheel via `uv tool install --force`, or `git pull --ff-only && uv sync --frozen` in a checkout). It only suggests: nothing is downloaded or installed. The check is one anonymous request to the public GitHub API (`api.github.com`) with a 3-second timeout, cached for 24 hours; offline it reports `unavailable`, and a failed request is not retried for 30 minutes (`force=true` at most once a minute). Even if the tool is never called, the server refreshes an expired cache on a background thread at startup, so it contacts `api.github.com` at most once a day; startup never waits for it. When the cache already knows of a newer release, the server adds a one-line hint to its MCP instructions at startup. Only plain version tags such as `v1.2.3` are reported. Set `TELEGRAM_MCP_DISABLE_UPDATE_CHECK=1` to turn all of this off, for example on an HTTP server without outbound internet access. Verify the wheel against `SHA256SUMS` as described in [INSTALL.md](INSTALL.md#install-a-release-package).

<details>
<summary>For developers: project checks</summary>

```sh
uv sync --frozen --all-groups
uv run --frozen python scripts/verify.py
uv run --frozen python scripts/verify_install.py
```

Tests use synthetic fixtures. A passing test run does not establish live provider connectivity.

</details>

## Built by ZAI.ONE

[ZAI.ONE](https://zai.one) is a digital agency working on websites, SEO, advertising and analytics. We also build tools that connect AI assistants to everyday work. [Talk to us on Telegram](https://t.me/zai_one) about setup, automation or an integration for your team.

## License, use and contributing

- **Use:** you may install and use this MCP server freely for yourself and your organisation, including commercial work. If it helps, please consider giving the repository a ⭐ — appreciated, never required.
- **Contribute:** [issues](https://github.com/zai-one/telegram-mcp/issues/new/choose) and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Never post tokens, cookies, sessions or account data.
- **Not allowed without written permission from ZAI.ONE:** copying code into other projects, forking for redistribution, embedding the server in other products or offering it as a competing service.
- License: [LicenseRef-ZAI-ONE 2.0](LICENSE) (source-available, not open source). Third-party notices and license history: [NOTICE](NOTICE).
