# Live event feed (`telegram_poll_events`, opt-in toolset `event_feed`)

Enable: `TELEGRAM_TOOLSETS=all,event_feed`. Unlike `telegram_poll_messages` (new ids only, one read per
chat) the feed listens to Telegram updates and reports **new, edited and deleted** messages in every chat.

1. Start: `telegram_poll_events()` -> `status=started`, `next_cursor`. Only events after this moment are seen.
2. Read: `telegram_poll_events(cursor=<next_cursor>, wait_seconds=0..55, chat_ids=[...]?, kinds=["new","edited","deleted"]?, limit<=200)`.
   Always continue with the returned `next_cursor` (it also skips filtered-out events). `has_more=true`: call again now.
3. `gap=true`: events may be missing. With `gap_window={from,to}` the server was down then: events from
   before the shutdown are still returned (ids only, `text_unavailable`) and messages sent during the
   downtime are backfilled as `new` events with `backfilled=true` (`backfilled_new`; if
   `backfill_complete=false` re-read that window). Edits/deletions during downtime cannot be recovered.
   Without `gap_window` (state lost or >2000 events) re-read chats with `telegram_get_conversation_context`.

Event fields: kind, chat_id, message_id (deleted: message_ids), sender_id, date, outgoing, has_media,
text (<=1000 chars, untrusted). Telegram omits chat_id for deletions in private chats and basic groups;
those events are hidden while a chat ACL is set or `chat_ids` is passed. Events carry numeric chat ids
only, so an ACL entry given as `@username` does not match feed events: list numeric ids in the ACL.

Accounts: one feed per account; pass the same `account=` with every call (a cursor names its account).
Only ids/dates are kept on disk (24 h, `TELEGRAM_FEED_PERSIST=0` to disable), never text; nothing is marked
read. Backfill reads up to 100 dialogs / 500 messages once per restart (`TELEGRAM_FEED_BACKFILL=0` to disable).
For a single awaited reply prefer
`telegram_wait_for_new_message`; for bursts `telegram_wait_for_settled_message` (skill telegram-watch-replies).
