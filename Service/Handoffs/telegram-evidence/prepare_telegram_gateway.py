"""Extract the original Telegram registration into a pinned-package bridge."""
import ast
import shutil
from pathlib import Path

root = Path(r"D:\ZAI\Infra\mcp-platform-telegram-services-extraction")
previous = Path(r"D:\ZAI\Infra\mcp-platform-services-extraction")
app = root / "src/mcp_platform/app.py"
source = app.read_text(encoding="utf-8")
assert "register_telegram_package" not in source
tree = ast.parse(source)
functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name.startswith(("telegram_", "_telegram_"))]
assert len(functions) == 19, [n.name for n in functions]
lines = source.splitlines(keepends=True)
for node in sorted(functions, key=lambda n: n.lineno, reverse=True):
    start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
    lines[start:node.end_lineno] = []
source = "".join(lines)
constants = [n for n in ast.parse(source).body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id.startswith("_TELEGRAM_") for t in n.targets)]
lines = source.splitlines(keepends=True)
for node in reversed(constants):
    lines[node.lineno - 1:node.end_lineno] = []
source = "".join(lines)
marker = "from mcp_platform.providers.telegram import TELEGRAM_WRITE_ALLOWLIST, TelegramAdapter\n"
assert marker in source
source = source.replace(marker, marker + "from mcp_platform.providers.telegram_package import register_telegram_package\n")
marker = '    @server.tool(auth=require_scopes("topvisor:read"))'
assert marker in source
source = source.replace(marker, '    register_telegram_package(server, registry, settings, store)\n\n' + marker, 1)
app.write_text(source, encoding="utf-8", newline="\n")
bridge = (previous / "src/mcp_platform/providers/arsenkin_bridge.py").read_text(encoding="utf-8")
bridge = bridge.replace("arsenkin", "telegram")
(root / "src/mcp_platform/providers/telegram_bridge.py").write_text(bridge, encoding="utf-8", newline="\n")
adapter = '''"""Compatibility types; real Telegram implementation and upstream live in the pinned wheel."""
from __future__ import annotations
import inspect
from typing import Any
from zai_telegram import adapter as implementation
from mcp_platform.providers.telegram_bridge import install_methods, translated

class TelegramAdapter(implementation.TelegramAdapter):
    pass

install_methods(TelegramAdapter, implementation.TelegramAdapter)

def __getattr__(name: str) -> Any:
    value = getattr(implementation, name)
    return translated(value) if inspect.isfunction(value) else value
'''
(root / "src/mcp_platform/providers/telegram.py").write_text(adapter, encoding="utf-8", newline="\n")
target = root / "scripts/prepare_service_artifacts.py"
assert not target.exists()
shutil.copyfile(previous / "scripts/prepare_service_artifacts.py", target)
(root / "ops/integrations").mkdir(exist_ok=True)
contract = root / "tests/fixtures/telegram-package-contract.json"
contract.parent.mkdir(exist_ok=True)
shutil.copyfile(Path(r"D:\ZAI\.tasks\verifications\mcp-services-extraction-evidence\telegram-baseline-contract.json"), contract)
print("Prepared Telegram package candidate bridge on isolated FastMCP4 baseline")
