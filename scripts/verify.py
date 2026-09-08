import subprocess
import sys

for command in (
    [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"],
    [sys.executable, "-m", "pytest", "-q"],
):
    subprocess.run(command, check=True)
