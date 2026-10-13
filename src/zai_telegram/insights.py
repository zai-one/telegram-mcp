"""Chat insight reads: forum topics, admins, channel/supergroup statistics."""

from __future__ import annotations

from typing import Any

from zai_telegram.tools import READ_ANNOTATIONS


def register_insights(server: Any, runtime: Any, call: Any) -> None:
    require_scopes = runtime.require_scopes

    @server.tool(auth=require_scopes("telegram:read"), annotations=READ_ANNOTATIONS)
    async def telegram_list_topics(chat_id: str, limit: int = 50, search_query: str | None = None) -> Any:
        """List forum topics (description in agent_layer.TOOL_DESCRIPTIONS)."""
        args: dict[str, Any] = {"chat_id": chat_id, "limit": limit}
        if search_query is not None:
            args["search_query"] = search_query
        return await call("telegram_list_topics", args)

    @server.tool(auth=require_scopes("telegram:read"), annotations=READ_ANNOTATIONS)
    async def telegram_get_admins(chat_id: str) -> Any:
        """List chat admins."""
        return await call("telegram_get_admins", {"chat_id": chat_id})

    @server.tool(auth=require_scopes("telegram:read"), annotations=READ_ANNOTATIONS)
    async def telegram_get_chat_stats(chat_id: str) -> Any:
        """Channel or supergroup statistics."""
        return await call("telegram_get_chat_stats", {"chat_id": chat_id})
