# Project development guidance

- Python 3.12+; Telegram bindings, correspondence and pinned upstream.
- No real messages/contacts in tests; preserve chat ACL, account bindings, frozen writes, quotas and durable outbox.
- Checks: `uv run python scripts/verify.py`; `python scripts/check_engineering.py --check`. Run install checks before release.

Read only the task-relevant public documentation. Personal workspace notes are optional and remain outside Git.

- [README](../../README.md): supported features and installation.
- [Contributing](../../CONTRIBUTING.md): development and release checks.
