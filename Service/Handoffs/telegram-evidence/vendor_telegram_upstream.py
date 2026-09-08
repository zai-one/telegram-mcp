"""Pin, patch and namespace reviewed upstream implementation; never execute Telegram API."""
import hashlib
import json
import subprocess
from pathlib import Path

upstream = Path('D:/ZAI/.tasks/work/mcp-services-extraction/telegram-upstream')
project = Path('D:/ZAI/MCP/Telegram')
package = project / 'src/zai_telegram'
target = package / '_vendor'
assert not target.exists()
pin = dict(line.split('=', 1) for line in (project / 'vendor/telegram-mcp.pin').read_text().splitlines() if line)
assert subprocess.check_output(['git', '-C', str(upstream), 'rev-parse', 'HEAD']).decode().strip() == pin['commit']
assert not subprocess.check_output(['git', '-C', str(upstream), 'status', '--porcelain']).strip()

def digest(path):
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()

checked = [('telegram_mcp/runtime.py', 'runtime'), ('telegram_mcp/install_guard.py', 'install_guard'),
           ('telegram_mcp/tools/messages.py', 'messages')]
for name, key in checked:
    assert digest(upstream / name) == pin['source_' + key + '_sha256'], name
patch = project / 'vendor/patches/telegram-mcp-v3.2.0-atg1.patch'
subprocess.run(['git', '-C', str(upstream), 'apply', '--check', '--ignore-whitespace', str(patch)], check=True)
subprocess.run(['git', '-C', str(upstream), 'apply', '--ignore-whitespace', str(patch)], check=True)
for name, key in checked:
    assert digest(upstream / name) == pin['patched_' + key + '_sha256'], name
target.mkdir(parents=True)
(target / '__init__.py').write_text('"""Pinned upstream implementation; see manifest.json and Apache-2.0 license."""\n', encoding='utf-8', newline='\n')
manifest = dict(repo=pin['repo'], commit=pin['commit'], tag=pin['tag'], upstream_distribution_version='2.0.1',
                patch_sha256=digest(patch), namespace='zai_telegram._vendor', files={})
names = subprocess.check_output(['git', '-C', str(upstream), 'ls-files', '-z']).decode().split('\0')
for name in names:
    if not (name.startswith('telegram_mcp/') and name.endswith('.py') or name == 'sanitize.py'):
        continue
    raw = (upstream / name).read_bytes().replace(b'\r\n', b'\n')
    value = raw.decode().replace('from telegram_mcp', 'from zai_telegram._vendor.telegram_mcp')
    value = value.replace('import telegram_mcp.', 'import zai_telegram._vendor.telegram_mcp.')
    value = value.replace('from sanitize import', 'from zai_telegram._vendor.sanitize import')
    path = target / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8', newline='\n')
    manifest['files'][name] = dict(patched_upstream_sha256=hashlib.sha256(raw).hexdigest(),
                                  packaged_sha256=hashlib.sha256(value.encode()).hexdigest())
(target / 'LICENSE').write_bytes((upstream / 'LICENSE').read_bytes())
(target / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8', newline='\n')
archive = project / 'vendor/upstream'
archive.mkdir()
for name in ['LICENSE', 'README.md', 'pyproject.toml']:
    (archive / name).write_bytes((upstream / name).read_bytes())
path = project / 'SOURCE_PROVENANCE.json'
data = json.loads(path.read_text(encoding='utf-8'))
data['upstream'] = dict(repo=pin['repo'], commit=pin['commit'], tag=pin['tag'], metadata_version='2.0.1',
                       manifest='src/zai_telegram/_vendor/manifest.json', license='Apache-2.0',
                       changes='Existing platform patch plus namespace-only import rewrites; adapter verifies packaged hashes before loading.')
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
adapter = package / 'adapter.py'
value = adapter.read_text(encoding='utf-8')
anchor = 'from zai_telegram.transport import ProviderError, ProviderTimeoutError, ProviderTransportError\n'
assert anchor in value
value = value.replace(anchor, anchor + 'from zai_telegram.vendor_integrity import assert_pinned_vendor\n')
old = '''                install_guard = importlib.import_module("telegram_mcp.install_guard")
                install_guard.assert_safe_distribution()
                runtime = importlib.import_module("telegram_mcp.runtime")
                importlib.import_module("telegram_mcp.tools")'''
new = '''                assert_pinned_vendor()
                runtime = importlib.import_module("zai_telegram._vendor.telegram_mcp.runtime")
                importlib.import_module("zai_telegram._vendor.telegram_mcp.tools")'''
assert old in value
adapter.write_text(value.replace(old, new), encoding='utf-8', newline='\n')
print(f'Vendored {len(manifest["files"])} upstream Python files with source/patch/package hashes; no Telegram execution')
