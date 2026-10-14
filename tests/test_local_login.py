import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def login():
    path = Path(__file__).parents[1] / "src/zai_telegram/login_telegram.py"
    spec = importlib.util.spec_from_file_location("local_login_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("two_step", [False, True])
async def test_login_saves_private_session_without_printing_secrets(
    login, two_step, tmp_path, monkeypatch, capsys
):
    calls = []

    class Client:
        session = type("Session", (), {"save": lambda _: "synthetic-session-value"})()

        def __init__(self, *args):
            pass

        async def connect(self):
            calls.append("connect")

        async def disconnect(self):
            calls.append("disconnect")

        async def send_code_request(self, phone):
            assert phone == "synthetic-phone"
            return type("Code", (), {"phone_code_hash": "synthetic-code-hash"})()

        async def sign_in(self, **kwargs):
            if two_step and "password" not in kwargs:
                raise login.SessionPasswordNeededError(request=None)
            calls.append("signed-in")

    monkeypatch.setattr(login, "TelegramClient", Client)
    monkeypatch.setattr("builtins.input", lambda _: "12345")
    values = iter(["synthetic-hash", "synthetic-phone", "synthetic-code", "synthetic-2fa"])
    monkeypatch.setattr(login.getpass, "getpass", lambda _: next(values))
    destination = tmp_path / "session.env"
    await login.create_session(destination)
    assert calls == ["connect", "signed-in", "disconnect"]
    text = destination.read_text()
    assert "TELEGRAM_SESSION_STRING=synthetic-session-value" in text
    assert "synthetic-2fa" not in text and "synthetic-phone" not in text
    captured = capsys.readouterr()
    assert "synthetic" not in captured.out + captured.err


async def test_login_never_overwrites_existing_session(login, tmp_path):
    destination = tmp_path / "session.env"
    destination.write_text("existing")
    with pytest.raises(ValueError):
        await login.create_session(destination)
    assert destination.read_text() == "existing"


@pytest.mark.parametrize("two_step", [False, True])
async def test_add_labeled_account_to_existing_files(two_step, tmp_path, monkeypatch, capsys):
    import json

    from zai_telegram import login_telegram, setup
    from zai_telegram.secrets import read_secret_env

    root = tmp_path / "setup"
    (root / "secrets").mkdir(parents=True)
    secret = root / "secrets/telegram.env"
    secret.write_text("TELEGRAM_API_ID=1\nTELEGRAM_API_HASH=synthetic-hash\nTELEGRAM_SESSION_STRING=old\n")
    bindings = root / "secrets/bindings.json"
    bindings.write_text(json.dumps({"local-operator": "default"}))
    for path in (secret, bindings):
        path.chmod(0o600)
    (root / "mcp.local.json").write_text(
        json.dumps(
            {
                "env": {
                    "TELEGRAM_SECRET_FILE": "secrets/telegram.env",
                    "TELEGRAM_BINDINGS_FILE": "secrets/bindings.json",
                }
            }
        )
    )

    class Client:
        session = type("Session", (), {"save": lambda _: "synthetic-work-session"})()

        def __init__(self, *args):
            pass

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def send_code_request(self, phone):
            return type("Code", (), {"phone_code_hash": "h"})()

        async def sign_in(self, **kwargs):
            if two_step and "password" not in kwargs:
                raise login_telegram.SessionPasswordNeededError(request=None)

    monkeypatch.setattr(login_telegram, "TelegramClient", Client)
    values = iter(["synthetic-phone", "synthetic-code", "synthetic-2fa"])
    monkeypatch.setattr(login_telegram.getpass, "getpass", lambda _: next(values))
    found_secret, found_bindings = setup.setup_paths(root)
    assert await login_telegram.add_account(found_secret, "work", bindings_path=found_bindings)
    values_now = read_secret_env(secret)
    assert values_now["TELEGRAM_SESSION_STRING_WORK"] == "synthetic-work-session"
    assert values_now["TELEGRAM_SESSION_STRING"] == "old"
    assert json.loads(bindings.read_text())["local-operator"] == ["default", "work"]
    assert "synthetic-2fa" not in secret.read_text()
    with pytest.raises(ValueError):
        await login_telegram.add_account(found_secret, "work", bindings_path=found_bindings)
    captured = capsys.readouterr()
    assert "synthetic" not in captured.out + captured.err
