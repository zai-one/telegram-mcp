"""Build an exact service revision from a clean local clone and compare its wheel."""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--manifest', type=Path, required=True)
parser.add_argument('--destination-name', required=True)
parser.add_argument('--extra', action='append', default=[])
args = parser.parse_args()
manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
root = Path('D:/ZAI/.tasks/work/mcp-services-extraction').resolve()
destination = (root / args.destination_name).resolve()
if destination.exists() or not destination.is_relative_to(root):
    raise SystemExit('destination must be a new owned task directory')

def run(*command, cwd=destination):
    subprocess.run([str(part) for part in command], cwd=cwd, check=True)

run('git', 'clone', '--no-hardlinks', '--no-checkout', args.source, destination, cwd=root)
# Rebuild the canonical Git blobs, including revisions predating .gitattributes.
# A machine-global Windows autocrlf setting must not alter wheel payload bytes.
run('git', 'config', 'core.autocrlf', 'false')
run('git', 'checkout', '--detach', manifest['source_revision'])
run('uv', 'venv', '--python', 'D:/ZAI/Infra/mcp-platform/.venv/Scripts/python.exe')
extras = [part for extra in args.extra for part in ('--extra', extra)]
run('uv', 'sync', '--frozen', '--all-groups', *extras)
run('uv', 'run', 'python', 'scripts/verify.py')
run('uv', 'build')
wheel = destination / 'dist' / manifest['wheel']
actual = hashlib.sha256(wheel.read_bytes()).hexdigest()
assert actual == manifest['sha256'], (actual, manifest['sha256'])
assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=destination).strip()
print('PASS clean clone, frozen dependencies, service verification, reproducible wheel SHA-256:', actual)
