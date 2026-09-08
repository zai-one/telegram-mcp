# Telegram MCP

Independent source for Telegram MCP adapter, correspondence tools and pinned upstream packaging.
Follow the ZAI task protocol within that workspace; runtime must work outside it.
Use Python and offline synthetic fixtures; tests never read real Telegram messages/contacts or send messages.
Preserve account bindings, scopes, frozen write allowlist, quotas, audit and durable idempotency.
No push, publication, deployment or real credential/session transfer in this extraction.
