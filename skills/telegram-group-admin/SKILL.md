---
name: telegram-group-admin
description: Administer Telegram groups and channels safely - bans, admin rights, permissions, slow mode, invites - with an explicit change plan and approval. Use for ban user, promote admin, права в группе, модерация чата.
license: LicenseRef-ZAI-ONE
compatibility: Requires zai-telegram-mcp with TELEGRAM_WRITE_ENABLED=true and toolset groups_admin.
metadata:
  mcp-server: zai-telegram-mcp
---

# Telegram group admin

1. Resolve the group (`telegram_get_chat`) and every user (`telegram_resolve_recipient`); stop on ambiguity.
2. Write the change plan with `templates/change-plan.md`: action, target, reversible?, upstream tool.
3. Get an explicit "yes" for the whole plan.
4. Execute one `telegram_write(tool, arguments, idempotency_key)` per row; stop at the first error and report.
5. Confirm results with `telegram_get_chat` / participants where possible.

Guardrails: high-risk tier (3/min); prefer reversible actions; never leave/delete a chat the user did not name. Tool names and arguments: `references/permissions.md`.
