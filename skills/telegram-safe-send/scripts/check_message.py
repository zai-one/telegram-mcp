#!/usr/bin/env python3
"""Check a Telegram message before sending: length and HTML tag balance.

Usage: python check_message.py [--html] < message.txt   (stdlib only)
"""

import re
import sys

LIMIT = 4096
TAGS = ("b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "blockquote", "tg-spoiler")


def check(text: str, html: bool) -> list[str]:
    problems = []
    if not text.strip():
        problems.append("message is empty")
    if len(text) > LIMIT:
        problems.append(f"too long: {len(text)} > {LIMIT}; split at paragraph boundaries")
    if html:
        for tag in TAGS:
            opened = len(re.findall(rf"<{tag}(\s[^>]*)?>", text))
            closed = text.count(f"</{tag}>")
            if opened != closed:
                problems.append(f"unbalanced <{tag}>: {opened} opened, {closed} closed")
        if re.search(r"&(?!(amp|lt|gt|quot|#\d+);)", text):
            problems.append("raw '&' must be escaped as &amp;")
    return problems


if __name__ == "__main__":
    found = check(sys.stdin.read(), "--html" in sys.argv[1:])
    print("\n".join(found) if found else "ok")
    sys.exit(1 if found else 0)
