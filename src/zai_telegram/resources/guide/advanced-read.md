# Advanced reads (`telegram_read`, opt-in toolset `advanced_read`)

Enable: `TELEGRAM_TOOLSETS=all,advanced_read` (or add `advanced_read` to your list). Call
`telegram_read(tool="<name>", arguments={...})`. Read-only; arguments as below (`account` is injected).
Prefer a dedicated tool when one exists (inbox, context, search, topics, admins, stats).

| Need | tool | arguments |
|---|---|---|
| Members of a group | get_participants | chat_id, page, page_size<=200 |
| Pinned messages | get_pinned_messages | chat_id |
| Reactions on a post | get_message_reactions | chat_id, message_id, limit |
| Who read my message | get_message_read_by | chat_id, message_id |
| Link to a message | get_message_link | chat_id, message_id |
| Messages around one id | get_message_context | chat_id, message_id, context_size |
| Filter by date/text | list_messages | chat_id, limit, search_query, from_date, to_date |
| Scheduled / drafts | get_scheduled_messages (chat_id), get_drafts | |
| Inline buttons | list_inline_buttons | chat_id, message_id |
| Media info | get_media_info | chat_id, message_id |
| Full chat / username | get_full_chat (chat_id), resolve_username (username) | |
| Common chats with a user | get_common_chats | user_id |
| Banned users / admin log | get_banned_users, get_recent_actions | chat_id |
| Folders | list_folders, get_folder (folder_id) | |
| Contacts | list_contacts, get_contact_ids, get_contact_chats (contact_id), get_last_interaction (contact_id), get_direct_chat_by_contact (contact_query), get_blocked_users | |
| Profiles | get_me, get_full_user (username), get_user_status (user_id), get_user_photos (user_id), get_bot_info (bot_username), get_privacy_settings | |
| Other | get_chats, list_chats, get_chat, search_public_chats, search_messages, search_global, get_history, list_topics, get_chat_stats, get_sticker_sets | |

With a chat allowlist (`TELEGRAM_ALLOWED_CHATS`) only calls that name a permitted chat/user are allowed;
account-wide reads (contacts, drafts, global search, folders) are refused with `chat_access_denied`.
With any chat ACL (allow or deny list), get_common_chats and get_contact_chats are refused too: they list
the user's other chats, which may be outside the ACL.
