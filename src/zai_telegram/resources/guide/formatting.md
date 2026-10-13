# Telegram formatting (parse_mode)

- `parse_mode=null` (default): plain text, safest; nothing is interpreted.
- `parse_mode="html"`: `<b>`, `<i>`, `<u>`, `<s>`, `<code>`, `<pre>`, `<a href="...">`, `<blockquote>`, `<tg-spoiler>`. Escape `<`, `>`, `&` in user text as `&lt;` `&gt;` `&amp;`.
- `parse_mode="md"`/`"markdown"`: Telethon markdown: `**bold**`, `__italic__`, `` `code` ``, fenced pre, `[text](url)`. Unbalanced markers break the message - prefer HTML for generated text.
- Limit: 4096 characters per message (captions 1024). Split long posts at paragraph boundaries, never inside a tag.
- Telegram previews the first link. `@username` mentions work in plain text.
- Always show the user the final text and parse_mode before sending to a channel or group.
