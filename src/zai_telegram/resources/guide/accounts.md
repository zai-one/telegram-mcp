# Several accounts and waiting for messages

Accounts
- `telegram_list_accounts` lists the labels you may use; the first is the default.
- Pass `account="<label>"` on any tool to act as that account; omit it for the default.
- Labels not granted to you fail with `account_not_granted` before any Telegram call.
- Limits, poll cursors and the outbox are per account. Say which account you used when you report.

Waiting / watching
- Wait for a reply: `telegram_wait_for_new_message(chat_ids=[...], timeout_seconds<=55)`. Returns
  `status=new_messages` with a `batch_id`, or `status=timeout`. Loop while the user wants to keep waiting.
- Wait for a settled burst: `telegram_wait_for_settled_message(chat_ids=[...], settle_seconds=6, max_wait_seconds<=55)`.
  Poll-based debounce — returns when a chat has had no new messages for `settle_seconds`
  (`status=settled`), or `status=partial` with pending messages if the deadline hits mid-burst,
  or `status=timeout` if nothing arrived. Prefer this when someone may send several messages in a row.
  Reads are paced like the plain wait (polls >= 2.5 s x chats apart, ~24 reads/min), so the quiet
  window is never shorter than that interval. Unread messages already waiting in watched chats are returned.
- After processing a batch call `telegram_acknowledge_poll(batch_id)`, otherwise the same messages return.
  Acknowledge with the same `account` as the poll/wait that returned the batch: batches and cursors are
  per account, so another account does not find the batch.
- One-shot check without waiting: `telegram_poll_messages` (first call only sets a baseline).
- Numeric chat ids only (get them with `telegram_list_chats`). 1-10 chats per call. Edits and deletions
  are not tracked. Nothing is marked read.
