# Telegram MCP maintenance

Independent source for Telegram MCP adapter, correspondence tools and pinned upstream packaging.
Follow the ZAI task protocol within that workspace; runtime must work outside it.
Use Python and offline synthetic fixtures; tests never read real Telegram messages/contacts or send messages.
Preserve account bindings, scopes, frozen write allowlist, quotas, audit and durable idempotency.

Use Python for scripts. Preserve existing MCP names/schemas and server-owned
authorization, account boundaries, budgets, approvals and unknown-outcome state.
Do not use live provider accounts or credentials in tests. Keep setup local and explicit.
Run scripts/verify.py and scripts/verify_install.py before releases.
Use Issues for requested changes. Preserve LICENSE, NOTICE and third-party licenses.
Never publish operational handoffs, private extraction refs, credentials or runtime state.
Production deployment is a separate action.
When operating in a workspace with a task/verifier protocol, follow that protocol.
