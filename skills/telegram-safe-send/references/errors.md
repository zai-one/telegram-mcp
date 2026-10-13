# Telegram MCP errors

Every failure is a JSON envelope: `{"code", "provider", "retryable", "hint", "docs", "retry_after_seconds"?}`.
Retry only when `retryable` is true and follow `hint`. Never re-send a write under a new idempotency_key unless the old one is `failed` in `telegram_outbox_status`.

| code | retryable | action |
|---|---|---|
| provider_rate_limited | yes | FLOOD_WAIT or local limit: wait `retry_after_seconds`, retry once (see flood-wait). |
| provider_timeout | yes | Retry once with the SAME idempotency_key; the ledger deduplicates. |
| provider_transport_failed | yes | Network issue; retry later with the same idempotency_key. |
| provider_temporarily_unavailable | yes | Retry later. |
| provider_authentication_failed | no | Session expired: the user re-runs `zai-telegram-login`. |
| provider_resource_not_found | no | Re-resolve IDs (telegram_list_chats / telegram_resolve_recipient). |
| provider_payload_too_large | no | Lower limit/page_size or split the text. |
| provider_request_invalid, validation_failed | no | Fix arguments per the schema. |
| provider_disabled | no | Operator disabled Telegram, writes or the account. Tell the user. |
| chat_access_denied | no | Chat outside TELEGRAM_ALLOWED_CHATS or in TELEGRAM_DENIED_CHATS. Ask the user; never work around it. |
| tool_disabled | no | Toolset or write category is off; see telegram://toolsets. |
| operation_failed, provider_response_invalid, provider_http_error | no | Report to the user. |

A write that failed with a transport error stays `pending`: check `telegram_outbox_status` before re-sending.
