"""Extract Telegram adapter and correspondence tools from committed source only."""
import ast
import hashlib
import json
import subprocess
import textwrap
from pathlib import Path

source = Path('D:/ZAI/Infra/mcp-platform')
target = Path('D:/ZAI/MCP/Telegram')
revision = 'f1a27960405bc73ca790d5d2694b47ad8c3a77b3'
assert not target.exists()
manifest = dict(source_repository='git@git.zai.one:zai.one/mcp-platform.git', source_revision=revision,
                runtime_status='extraction in progress; not a release', files=[], functions=[])

def read(path):
    return subprocess.check_output(['git', '-C', str(source), 'show', f'{revision}:{path}']).decode()

def write(path, value):
    path = target / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8', newline='\n')

def convert(value):
    for old, new in {
        'mcp_platform.providers.telegram': 'zai_telegram.adapter',
        'mcp_platform.providers.base': 'zai_telegram.transport',
        'mcp_platform.response_sanitizer': 'zai_telegram.sanitizer',
        'mcp_platform.secrets': 'zai_telegram.secrets',
    }.items():
        value = value.replace(old, new)
    return value

def copied(old, new):
    value = read(old)
    transformed = convert(value)
    write(new, transformed)
    manifest['files'].append(dict(source=old, destination=new,
        source_sha256=hashlib.sha256(value.encode()).hexdigest(),
        initial_extracted_sha256=hashlib.sha256(transformed.encode()).hexdigest()))

def segment(value, node):
    first = min([node.lineno, *(n.lineno for n in getattr(node, 'decorator_list', []))])
    return textwrap.dedent(''.join(value.splitlines(keepends=True)[first-1:node.end_lineno]))

for old, new in {
    'src/mcp_platform/providers/telegram.py': 'src/zai_telegram/adapter.py',
    'src/mcp_platform/providers/base.py': 'src/zai_telegram/transport.py',
    'src/mcp_platform/secrets.py': 'src/zai_telegram/secrets.py',
    'src/mcp_platform/error_envelope.py': 'src/zai_telegram/errors.py',
    'src/mcp_platform/response_sanitizer.py': 'src/zai_telegram/sanitizer.py',
    'tests/test_response_sanitizer.py': 'tests/test_sanitizer.py',
    'tests/providers/test_telegram_runtime_errors.py': 'tests/test_adapter_runtime_errors.py',
    'vendor/telegram-mcp.pin': 'vendor/telegram-mcp.pin',
    'vendor/patches/telegram-mcp-v3.2.0-atg1.patch': 'vendor/patches/telegram-mcp-v3.2.0-atg1.patch',
}.items():
    copied(old, new)

value = read('src/mcp_platform/app.py')
tree = ast.parse(value)
header = '''from __future__ import annotations
import json
import re
from datetime import datetime
from typing import Any
from zai_telegram.adapter import TelegramAdapter
from zai_telegram.transport import ProviderError, ProviderRateLimited, request_hash
from zai_telegram.errors import SafeToolError, safe_provider_error
from zai_telegram.sanitizer import sanitize_provider_response
'''
tools = header
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name.startswith('_telegram_'):
        tools += '\n\n' + segment(value, node)
    if isinstance(node, ast.Assign) and any(isinstance(n, ast.Name) and n.id.startswith('_TELEGRAM_') for n in node.targets):
        tools += '\n\n' + segment(value, node)
factory = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'create_platform')
nodes = [n for n in factory.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name.startswith(('telegram_', '_telegram_'))]
assert len([n for n in nodes if n.name.startswith('telegram_')]) == 12
assert {n.name for n in nodes if n.name.startswith('_')} == {'_telegram_call', '_telegram_write'}
tools += '''

def register_tools(server: Any, runtime: Any) -> None:
    registry, settings, store = runtime.registry, runtime.settings, runtime.store
    current_access, require_scopes = runtime.current_access, runtime.require_scopes
'''
for node in nodes:
    raw = segment(value, node)
    tools += '\n' + textwrap.indent(raw, '    ')
    manifest['functions'].append(dict(source='src/mcp_platform/app.py', name=node.name,
                                      source_sha256=hashlib.sha256(raw.encode()).hexdigest()))
write('src/zai_telegram/tools.py', tools)
write('src/zai_telegram/__init__.py', '__version__ = "0.1.0"\n')
write('src/zai_telegram/py.typed', '')
write('.gitattributes', '* text=auto eol=lf\n*.whl binary\n*.gz binary\n')
write('.gitignore', '.venv/\n__pycache__/\n.pytest_cache/\n.ruff_cache/\ndist/\n*.egg-info/\nstate/\n.env\n*.sqlite*\n*.session*\n')
write('AGENTS.md', '''# Telegram MCP

Independent source for Telegram MCP adapter, correspondence tools and pinned upstream packaging.
Follow the ZAI task protocol within that workspace; runtime must work outside it.
Use Python and offline synthetic fixtures; tests never read real Telegram messages/contacts or send messages.
Preserve account bindings, scopes, frozen write allowlist, quotas, audit and durable idempotency.
No push, publication, deployment or real credential/session transfer in this extraction.
''')
write('README.md', '''# Telegram MCP

Самостоятельное выделение Telegram из платформы. Перенесены реальные адаптер
и 12 MCP-инструментов переписки с исходными проверками и постоянным журналом
через внедряемое хранилище. Закреплённые upstream и патчи учитываются отдельно.
Standalone runtime/state и упаковка upstream ещё разрабатываются; это не release.

Исходники: SOURCE_PROVENANCE.json. Только локальные synthetic fixtures;
реальные sessions, сообщения, контакты и сетевые вызовы Telegram не используются.
''')
write('pyproject.toml', '''[build-system]
requires = ["hatchling==1.28.0"]
build-backend = "hatchling.build"

[project]
name = "zai-telegram-mcp"
version = "0.1.0"
description = "Standalone governed Telegram MCP with pinned upstream"
readme = "README.md"
requires-python = ">=3.12,<3.15"
dependencies = [
  "fastmcp==4.0.0",
  "httpx==0.28.1",
  "httpx2==2.12.0",
  "nest-asyncio==1.6.0",
  "python-dotenv==1.2.2",
  "python-json-logger==3.3.0",
  "qrcode==8.2",
  "telethon==1.44.0",
]

[dependency-groups]
dev = ["pytest==9.1.1", "pytest-asyncio==1.4.0", "ruff==0.15.21"]

[tool.hatch.build.targets.wheel]
packages = ["src/zai_telegram"]

[tool.pytest.ini_options]
addopts = "-ra --strict-config --strict-markers"
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.ruff]
target-version = "py312"
line-length = 110
extend-exclude = ["src/zai_telegram/_vendor"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "ASYNC"]
''')
write('scripts/verify.py', '''import subprocess
import sys

for command in ([sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"],
                [sys.executable, "-m", "pytest", "-q"]):
    subprocess.run(command, check=True)
''')
write('SOURCE_PROVENANCE.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
subprocess.run(['git', 'init', '-b', 'main', str(target)], check=True)
for name, value in [('user.name', 'Codex'), ('user.email', 'codex@local.invalid'), ('core.autocrlf', 'false')]:
    subprocess.run(['git', '-C', str(target), 'config', name, value], check=True)
print('Created Telegram source repo with 12 public tools and original pinned upstream inputs')
