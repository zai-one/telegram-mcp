"""Import tool modules so their MCP decorators register with the shared server."""

from zai_telegram._vendor.telegram_mcp.tools.accounts import *
from zai_telegram._vendor.telegram_mcp.tools.contacts import *
from zai_telegram._vendor.telegram_mcp.tools.chats import *
from zai_telegram._vendor.telegram_mcp.tools.messages import *
from zai_telegram._vendor.telegram_mcp.tools.groups import *
from zai_telegram._vendor.telegram_mcp.tools.media import *
from zai_telegram._vendor.telegram_mcp.tools.profile import *
from zai_telegram._vendor.telegram_mcp.tools.folders import *
from zai_telegram._vendor.telegram_mcp.tools.events import *

__all__ = [name for name in globals() if not name.startswith("_")]
