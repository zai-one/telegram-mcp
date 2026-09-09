import socket

import pytest

from zai_telegram.config import SCOPES, ServiceConfig


@pytest.fixture
def config(tmp_path):
    secret = tmp_path / "fixture.env"
    secret.write_text(
        "TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH=synthetic-api-hash\n"
        "TELEGRAM_SESSION_STRING=synthetic-session\n",
        encoding="utf-8",
    )
    secret.chmod(0o600)
    return ServiceConfig(
        state_path=tmp_path / "state.sqlite",
        secret_path=secret,
        bindings={"local-operator": "default", "alice": "default", "bob": "second"},
        local_scopes=SCOPES,
        telegram_write_enabled=True,
    )


@pytest.fixture(autouse=True)
def deny_external_network(monkeypatch):
    original = socket.socket.connect

    def local_only(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError("external network forbidden in extraction tests")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
