🇬🇧 English · [🇷🇺 Русский](README.ru.md)

# Telegram MCP

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

## Access and limits

Your session grants access to Telegram data, so use a client you trust. The assistant receives messages you request. Incomplete recipient searches remain ambiguous. [Media setup and message polling](docs/MEDIA_AND_POLLING.md) explain file permissions, size limits and acknowledgment. Polling tracks new message IDs; it does not track edits or deletions and is not a live event stream.

Authenticated HTTP is available for a server deployment. See [HTTP setup](INSTALL.md#http), [configuration and permissions](docs/RUNTIME.md) and [Python package integration](INSTALL.md#python-package-and-platform-integration).

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
