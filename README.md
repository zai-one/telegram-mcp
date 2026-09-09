🇬🇧 English · [🇷🇺 Русский](README.ru.md)

# Telegram MCP

MCP server for reading Telegram conversations and sending messages from an AI assistant. It connects to a Telegram user account and provides chat search, conversation context, recipient lookup and delivery status.

## What you can do

- Browse chats and inbox messages, search messages and retrieve conversation context.
- Resolve a recipient before sending a message or reply.
- Send message batches and inspect their outbox status when writing is enabled.

## Quick start

Install Python 3.12+ (below 3.15), [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git.

You need a Telegram API ID, API hash and an authenticated user session. The local wizard can create the session through an interactive login. A bot token cannot replace the user session. See [Telegram login setup](INSTALL.md#from-a-clone-or-source-zip).

```sh
git clone https://github.com/zai-one/telegram-mcp.git
cd telegram-mcp
uv sync --frozen
uv run --frozen python scripts/configure.py
uv run --frozen zai-telegram-mcp --config mcp.local.json --check-config
uv run --frozen zai-telegram-mcp --config mcp.local.json
```

The last command starts stdio and waits for an MCP client; it is not an interactive chat.
See [INSTALL.md](INSTALL.md) for credentials, client configuration, HTTP and package integration.
`--check-config` checks local settings only; it never validates a provider account over the network.

## Scope and limits

Read-only access is the default. Sending requires separate permission; an incomplete recipient search remains ambiguous and is not treated as a confirmed match. This interface does not include media transfers or a live event feed. See [access and messaging settings](docs/RUNTIME.md).

## Verification

```sh
uv sync --frozen --all-groups
uv run --frozen python scripts/verify.py
uv run --frozen python scripts/verify_install.py
```

Tests use synthetic fixtures. A passing test run does not establish live provider connectivity.

## Use and feedback

You may install and use this project for your own accounts under [LicenseRef-ZAI-ONE](LICENSE).
This is not an open-source license. Third-party notices remain in [NOTICE](NOTICE).
If it helps, give the repository a ⭐. Missing something or found a bug? [Open an issue](https://github.com/zai-one/telegram-mcp/issues/new/choose).
I'm working on this project; accepted improvements are implemented here. Support is not guaranteed.
