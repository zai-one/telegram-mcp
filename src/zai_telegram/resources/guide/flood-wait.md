# FLOOD_WAIT and rate limits

- Telegram answers bursts with FLOOD_WAIT_X (wait X seconds); the server maps it to `provider_rate_limited` with `retry_after_seconds`.
- Local ceilings: 30 reads/min, 20 writes/min, 3 high-risk writes/min (delete, admin, identity), 2 concurrent calls.
- Do: one telegram_get_inbox instead of many get_chat calls, small `limit`, telegram_poll_messages instead of loops.
- Don't: retry immediately, send in parallel, mass-message strangers (spam reports can restrict the account).
- Outreach: telegram_send_many (max 20 items, each idempotent), then telegram_outbox_status.
