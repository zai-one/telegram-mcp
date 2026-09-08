import hashlib
import json
import shutil
import subprocess
from pathlib import Path

service = Path(r"D:\ZAI\MCP\Telegram")
root = Path(r"D:\ZAI\Infra\mcp-platform-telegram-services-extraction")
evidence = Path(r"D:\ZAI\.tasks\verifications\mcp-services-extraction-evidence")
manifest = json.loads((evidence / "telegram-release-0.1.0.json").read_text(encoding="utf-8"))
assert not subprocess.check_output(["git", "-C", str(service), "status", "--porcelain"]).strip()
assert subprocess.check_output(["git", "-C", str(service), "rev-parse", "HEAD"]).decode().strip() == manifest["source_revision"]
wheel = service / "dist" / manifest["wheel"]
assert hashlib.sha256(wheel.read_bytes()).hexdigest() == manifest["sha256"]
destination = evidence / "release-artifacts" / wheel.name
assert not destination.exists()
shutil.copyfile(wheel, destination)
entry = {key: manifest[key] for key in ("package", "version", "wheel", "sha256", "source_revision")}
entry.update(name="telegram", status="committed local candidate; not deployed")
path = root / "ops/integrations/service-packages.json"
assert not path.exists()
path.write_text(json.dumps({"services": [entry]}, indent=2) + "\n", encoding="utf-8", newline="\n")
subprocess.run([str(root / ".venv/Scripts/python.exe"), "scripts/prepare_service_artifacts.py",
                "--artifact-directory", str(destination.parent)], cwd=root, check=True)
print(json.dumps(entry))
