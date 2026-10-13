🇬🇧 English · [🇷🇺 Русский](README.ru.md)

# Telegram MCP

<!-- mcp-name: io.github.zai-one/telegram-mcp -->

**Catch up on a conversation and prepare your reply in the same chat.**

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

## Quick start

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
claude mcp add telegram -- uvx --from git+https://github.com/zai-one/telegram-mcp@v0.4.0 zai-telegram-mcp --config /ABSOLUTE/PATH/mcp.local.json
# Claude Code plugin (MCP + skills) from this repository; set TELEGRAM_MCP_CONFIG to your mcp.local.json
/plugin marketplace add zai-one/telegram-mcp
/plugin install telegram@zai-telegram
```

Codex `~/.codex/config.toml`:

```toml
[mcp_servers.telegram]
command = "uvx"
args = ["--from", "git+https://github.com/zai-one/telegram-mcp@v0.4.0", "zai-telegram-mcp", "--config", "/ABSOLUTE/PATH/mcp.local.json"]
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

Toolsets: `core`, `messages_read`, `messages_write`, `events`, `chat_insights` (`telegram_list_topics`, `telegram_get_admins`, `telegram_get_chat_stats`), `advanced_write` (generic `telegram_write`), and write categories for `telegram_write`: `media`, `contacts`, `groups_admin`, `channels`, `folders_drafts`, `profile_privacy`, `stickers_reactions_polls`. Writes still need `TELEGRAM_WRITE_ENABLED=true` and the `telegram:write` scope. The assistant can read the live setup from `telegram://toolsets`. Errors are JSON with `code`, `retryable`, `hint` and `docs` (`telegram://guide/errors`).

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

## Use and feedback

You may install and use this project for your own accounts under [LicenseRef-ZAI-ONE](LICENSE).
This is not an open-source license. Third-party notices remain in [NOTICE](NOTICE).
If it helps, give the repository a ⭐. Missing something or found a bug? [Open an issue](https://github.com/zai-one/telegram-mcp/issues/new/choose).
I'm working on this project; accepted improvements are implemented here. Support is not guaranteed.
