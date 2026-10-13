# Group and channel admin actions

Admin actions go through `telegram_write` (category `groups_admin`); they are high-risk (3/min, audited).
- ban_user / unban_user(group_id, user_id), promote_admin / demote_admin / edit_admin_rights, set_default_chat_permissions, toggle_slow_mode, invite_to_group, leave_chat.
- Before each action: resolve group and user ids, state exactly what changes, get the user's explicit "yes", then call with a stable idempotency_key.
- Bulk: one call per user, stop at the first error.
- Prefer reversible actions (slow mode, permissions) over bans; never leave or delete a chat the user did not name.
