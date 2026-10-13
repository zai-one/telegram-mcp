# Chat access control and toolsets

- `TELEGRAM_ALLOWED_CHATS=-1001234567890,@team_chat`: only these chats can be read or written; listings hide others. Unset = all chats (backward-compatible default).
- `TELEGRAM_DENIED_CHATS=...`: always denied, even when allowed.
- With an allowlist, a call must name an allowed chat or it is denied (`chat_access_denied`). For the generic `telegram_write` the target is `chat_id`/`group_id`/`peer` etc.; if none is given, `user_id`/`user_ids` (the private chat) or `channel`. Users inside a named chat (ban, promote, invite) are members of that chat. Account-wide or chat-creating writes are always denied under an allowlist: create_group, create_channel, join_chat_by_link, import_chat_invite, create/delete/reorder folders, add/import contacts, profile and privacy settings, set_bot_commands, upload_file.
- Values are numeric ids (as returned by telegram_list_chats) or @usernames. A call is matched by the form it uses, so list both forms when in doubt.
- `TELEGRAM_READ_ONLY=1` hides every write tool. `TELEGRAM_TOOLSETS=default` keeps core, messages_read, messages_write, events. Unset = all.
- Write categories (messages_write, media, contacts, groups_admin, channels, folders_drafts, profile_privacy, stickers_reactions_polls) gate the generic `telegram_write`.
- The live configuration is at `telegram://toolsets`.
