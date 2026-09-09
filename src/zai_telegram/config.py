from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from zai_telegram.secrets import read_private_secret_env, require_private_file

SCOPES = frozenset({"telegram:read", "telegram:write"})


@dataclass(frozen=True)
class ServiceConfig:
    state_path: Path
    secret_path: Path
    bindings: Mapping[str, str] = field(default_factory=dict)
    account_id: str = "default"
    principal_id: str = "local-operator"
    public_key: str = ""
    issuer: str = "telegram-operator"
    audience: str = "telegram-mcp"
    local_scopes: frozenset[str] = frozenset({"telegram:read"})
    telegram_write_enabled: bool = False
    enabled: bool = True
    rate_limit: int = 30
    write_rate_limit: int = 20
    telegram_high_risk_rate_limit_per_minute: int = 3
    max_concurrency: int = 2

    def __post_init__(self):
        if not all(
            isinstance(v, str) and 1 <= len(v) <= 256
            for v in (self.account_id, self.principal_id, self.issuer, self.audience)
        ):
            raise ValueError("bounded identity fields required")
        if not self.local_scopes <= SCOPES:
            raise ValueError("unknown local scope")
        for value, maximum in (
            (self.rate_limit, 30),
            (self.write_rate_limit, 20),
            (self.telegram_high_risk_rate_limit_per_minute, 3),
            (self.max_concurrency, 2),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError("policy exceeds original default ceiling")
        if not isinstance(self.enabled, bool) or not isinstance(self.telegram_write_enabled, bool):
            raise ValueError("explicit boolean provider policy required")
        copied = dict(self.bindings)
        if any(
            not isinstance(actor, str)
            or not 1 <= len(actor) <= 256
            or not isinstance(label, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", label) is None
            for actor, label in copied.items()
        ):
            raise ValueError("invalid server-owned account binding")
        object.__setattr__(self, "bindings", MappingProxyType(copied))

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(
            value
            for key, value in read_private_secret_env(self.secret_path).items()
            if key == "TELEGRAM_API_HASH" or key.startswith("TELEGRAM_SESSION_STRING")
        )

    @classmethod
    def from_env(cls):
        secret = Path(os.environ.get("TELEGRAM_SECRET_FILE", "")).resolve()
        require_private_file(secret)
        binding_path = Path(os.environ.get("TELEGRAM_BINDINGS_FILE", "")).resolve()
        require_private_file(binding_path)
        bindings = json.loads(binding_path.read_text(encoding="utf-8"))
        if not isinstance(bindings, dict):
            raise ValueError("binding map required")
        public = os.environ.get("TELEGRAM_MCP_PUBLIC_KEY_FILE", "")
        write_mode = os.environ.get("TELEGRAM_WRITE_ENABLED", "false")
        enabled = os.environ.get("TELEGRAM_ENABLED", "true")
        if write_mode not in {"true", "false"} or enabled not in {"true", "false"}:
            raise ValueError("boolean policy must be true or false")
        return cls(
            state_path=Path(os.environ.get("TELEGRAM_STATE_PATH", "state/telegram.sqlite")),
            secret_path=secret,
            bindings=bindings,
            account_id=os.environ.get("TELEGRAM_ACCOUNT_ID", "default"),
            principal_id=os.environ.get("TELEGRAM_LOCAL_PRINCIPAL", "local-operator"),
            public_key=Path(public).read_text(encoding="utf-8") if public else "",
            issuer=os.environ.get("TELEGRAM_MCP_ISSUER", "telegram-operator"),
            audience=os.environ.get("TELEGRAM_MCP_AUDIENCE", "telegram-mcp"),
            local_scopes=frozenset(os.environ.get("TELEGRAM_LOCAL_SCOPES", "telegram:read").split()),
            telegram_write_enabled=write_mode == "true",
            enabled=enabled == "true",
            rate_limit=int(os.environ.get("TELEGRAM_RATE_LIMIT", "30")),
            write_rate_limit=int(os.environ.get("TELEGRAM_WRITE_RATE_LIMIT", "20")),
            telegram_high_risk_rate_limit_per_minute=int(
                os.environ.get("TELEGRAM_HIGH_RISK_RATE_LIMIT", "3")
            ),
            max_concurrency=int(os.environ.get("TELEGRAM_MAX_CONCURRENCY", "2")),
        )
