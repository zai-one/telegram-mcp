# Group and channel admin actions

Admin actions go through `telegram_write` (category `groups_admin`); they are high-risk (3/min, audited).
- ban_user / unban_user(group_id, user_id), promote_admin / demote_admin / edit_admin_rights, set_default_chat_permissions, toggle_slow_mode, invite_to_group, leave_chat.
- Before each action: resolve group and user ids, state exactly what changes, get the user's explicit "yes", then call with a stable idempotency_key.
- Bulk: one call per user, stop at the first error.
- Prefer reversible actions (slow mode, permissions) over bans; never leave or delete a chat the user did not name.
- remove_user(chat_id, user_id) kicks without a permanent ban; get_invite_link / export_chat_invite(chat_id) create invite links.
- Forum topics: create_forum_topic, edit_forum_topic(chat_id, topic_id, title?, closed?, hidden?), delete_forum_topic(chat_id, topic_id) (deletes all topic messages).
- Check rights first: telegram_read get_member_admin_status(chat_id, user_id).
- Other categories: vote_poll(chat_id, message_id, option_indexes) in stickers_reactions_polls; rename_folder(folder_id, title) in folders_drafts.
