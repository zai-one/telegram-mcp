"""Compatibility import; interactive login is shipped in the installed package."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from zai_telegram.login_telegram import create_session  # noqa: E402, F401
