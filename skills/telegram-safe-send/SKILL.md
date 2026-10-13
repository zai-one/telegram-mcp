---
name: telegram-safe-send
description: Send or post a Telegram message safely - resolve the recipient exactly, draft, preview, get explicit approval, send once with an idempotency key, verify. Use for "напиши в Telegram", publish channel post, reply, outreach batch.
license: LicenseRef-ZAI-ONE
compatibility: Requires zai-telegram-mcp with TELEGRAM_WRITE_ENABLED=true and toolset messages_write.
metadata:
  mcp-server: zai-telegram-mcp
---

# Telegram safe send

1. Resolve: `telegram_resolve_recipient(query)`. Proceed only on `status="unique"`; on `ambiguous` show candidates and ask; never guess a chat_id.
2. Draft the text. Check length/format with `scripts/check_message.py` (4096 chars, HTML balance) and `references/formatting.md`.
3. Preview with `templates/preview.md` and ask for an explicit "yes" (recipient + exact text + parse_mode).
4. Send: `telegram_send_message(chat_id, message, parse_mode, idempotency_key)`; reply: `telegram_reply_to_message`; many recipients: `telegram_send_many` (<=20, unique keys).
5. Verify: `telegram_outbox_status(idempotency_keys=[...])`; report message ids.

Guardrails
- One approval = one send. Edits after approval need a new preview.
- Reuse the same idempotency_key on retry; it prevents duplicates.
- Errors: follow `hint`; retry only if `retryable` (`references/errors.md`, `references/flood-wait.md`).
- Never message people who did not opt in; no bulk cold outreach.
