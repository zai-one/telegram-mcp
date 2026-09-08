"""Verify an exact local gateway revision from a new clone and pinned artifacts."""
import argparse
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--branch', required=True)
parser.add_argument('--revision', required=True)
parser.add_argument('--destination-name', required=True)
parser.add_argument('--test-keyword', required=True)
args = parser.parse_args()
root = Path('D:/ZAI/.tasks/work/mcp-services-extraction').resolve()
destination = (root / args.destination_name).resolve()
if destination.exists() or not destination.is_relative_to(root):
    raise SystemExit('destination must be a new owned task directory')
def run(*command, cwd=destination):
    subprocess.run([str(x) for x in command], cwd=cwd, check=True)
run('git', 'clone', '--no-hardlinks', '--single-branch', '--branch', args.branch,
    args.source, destination, cwd=root)
assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=destination).decode().strip() == args.revision
run('python', 'scripts/prepare_service_artifacts.py', '--artifact-directory',
    'D:/ZAI/.tasks/verifications/mcp-services-extraction-evidence/release-artifacts')
run('uv', 'venv', '--python', 'D:/ZAI/Infra/mcp-platform/.venv/Scripts/python.exe')
run('uv', 'sync', '--frozen', '--all-groups')
run('uv', 'run', 'pytest', '-q', '-k', args.test_keyword)
run('uv', 'build')
assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=destination).strip()
print('PASS clean clone, exact commit, explicit pinned wheel, frozen install, targeted tests and build')
