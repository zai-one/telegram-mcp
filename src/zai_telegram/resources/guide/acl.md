# Chat access control and toolsets

- `TELEGRAM_ALLOWED_CHATS=-1001234567890,@team_chat`: only these chats can be read or written; listings hide others. Unset = all chats (backward-compatible default).
- `TELEGRAM_DENIED_CHATS=...`: always denied, even when allowed.
- Values are numeric ids (as returned by telegram_list_chats) or @usernames. A call is matched by the form it uses, so list both forms when in doubt.
- `TELEGRAM_READ_ONLY=1` hides every write tool. `TELEGRAM_TOOLSETS=default` keeps core, messages_read, messages_write, events. Unset = all.
- Write categories (messages_write, media, contacts, groups_admin, channels, folders_drafts, profile_privacy, stickers_reactions_polls) gate the generic `telegram_write`.
- The live configuration is at `telegram://toolsets`.
