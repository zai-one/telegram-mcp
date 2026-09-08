"""Clean locked wheel install and independent stdio/HTTP probes without provider calls."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class ClosingDirectory(tempfile.TemporaryDirectory):
    def cleanup(self) -> None:
        # Windows may release a terminated process's inherited log handle just
        # after wait() completes. Retry only deletion of this owned temp directory.
        target = Path(self.name).resolve()
        parent = Path(tempfile.gettempdir()).resolve()
        if not target.is_relative_to(parent) or not target.name.startswith("telegram-wheel-probe-"):
            raise ValueError("temporary cleanup escaped the owned probe directory")
        for attempt in range(50):
            try:
                super().cleanup()
                return
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.1)


async def probe() -> None:
    import httpx2 as httpx
    import zai_telegram
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport
    from fastmcp.exceptions import ToolError
    from fastmcp.server.auth.providers.jwt import RSAKeyPair

    assert Path(zai_telegram.__file__).is_relative_to(Path(sys.prefix))
    assert importlib.util.find_spec("mcp_platform") is None
    package = Path(zai_telegram.__file__).parent
    assert (package / "_contracts/telegram.json").is_file()
    from zai_telegram.vendor_integrity import assert_pinned_vendor
    assert_pinned_vendor()
    with ClosingDirectory(prefix="telegram-wheel-probe-") as directory:
        root = Path(directory)
        credential = root / "fixture.env"
        credential.write_text("TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH=synthetic-api-hash\n"
                              "TELEGRAM_SESSION_STRING=synthetic-session\n")
        credential.chmod(0o600)
        bindings = root / "bindings.json"
        bindings.write_text(json.dumps({"local-operator": "default", "probe": "default"}))
        bindings.chmod(0o600)
        env = {"TELEGRAM_SECRET_FILE": str(credential), "TELEGRAM_BINDINGS_FILE": str(bindings),
               "TELEGRAM_STATE_PATH": str(root / "state.sqlite"), "TELEGRAM_WRITE_ENABLED": "false",
               "TELEGRAM_LOCAL_SCOPES": "telegram:read telegram:write",
               "TELEGRAM_ACCOUNT_ID": "default", "TELEGRAM_MCP_ISSUER": "telegram-operator",
               "TELEGRAM_MCP_AUDIENCE": "telegram-mcp"}
        transport = StdioTransport(command=sys.executable, args=["-m", "zai_telegram"],
                                   cwd=str(root), env=env, keep_alive=False)
        async with Client(transport, timeout=30) as client:
            assert client.server_info.version == "0.1.0"
            assert len(await client.list_tools()) == 12
            try:
                await client.call_tool("telegram_get_chat", {"chat_id": ""})
            except ToolError as exc:
                assert "provider_error" in str(exc)
            else:
                raise AssertionError("invalid project accepted")
        pair = RSAKeyPair.generate()
        public = root / "public.pem"
        public.write_text(pair.public_key)
        env["TELEGRAM_MCP_PUBLIC_KEY_FILE"] = str(public)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with (root / "http.log").open("w") as log:
            process = subprocess.Popen([sys.executable, "-m", "zai_telegram", "--transport", "http",
                                        "--host", "127.0.0.1", "--port", str(port)],
                                       cwd=root, env={**os.environ, **env}, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                for _ in range(300):
                    if process.poll() is not None:
                        raise AssertionError("HTTP entrypoint exited before readiness")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        await asyncio.sleep(0.1)
                else:
                    raise AssertionError("HTTP entrypoint did not become ready")
                url = f"http://127.0.0.1:{port}/mcp"
                async with httpx.AsyncClient() as http:
                    denied = await http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                    assert denied.status_code in {401, 403}
                bearer = pair.create_token(subject="probe", issuer="telegram-operator", audience="telegram-mcp",
                                            scopes=["telegram:read"], expires_in_seconds=60,
                                            additional_claims={"account_id": "default"})
                async with Client(url, auth=bearer, timeout=30) as client:
                    assert client.server_info.version == "0.1.0"
                    assert len(await client.list_tools()) == 7
                    try:
                        await client.call_tool("telegram_get_chat", {"chat_id": ""})
                    except ToolError as exc:
                        assert "provider_error" in str(exc)
                    else:
                        raise AssertionError("HTTP invalid project accepted")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                for _ in range(50):
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            pass
                    except OSError:
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("owned HTTP fixture port remained open after termination")
    print("PASS clean wheel import without mcp_platform; real stdio and authenticated HTTP entrypoints")


def main() -> None:
    if sys.argv[1:] == ["--probe"]:
        asyncio.run(probe())
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.environment.exists() or args.manifest.exists():
        raise SystemExit("create-only: environment/manifest already exists")
    wheel = args.service / "dist/zai_telegram_mcp-0.1.0-py3-none-any.whl"
    requirements = args.environment.parent / (args.environment.name + "-requirements.txt")
    python = args.environment / "Scripts/python.exe"
    commands = [
        ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "-o", str(requirements)],
        ["uv", "venv", "--python", str(args.service / ".venv/Scripts/python.exe"), str(args.environment)],
        ["uv", "pip", "install", "--python", str(python), "--require-hashes", "-r", str(requirements)],
        ["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
        [str(python), str(Path(__file__).resolve()), "--probe"],
    ]
    for command in commands:
        subprocess.run(command, cwd=args.service, check=True)
    manifest = {"package": "zai-telegram-mcp", "version": "0.1.0", "wheel": wheel.name,
                "source_revision": subprocess.check_output(["git", "-C", str(args.service), "rev-parse", "HEAD"]).decode().strip(),
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "uv_lock_sha256": hashlib.sha256((args.service / "uv.lock").read_bytes()).hexdigest(),
                "verification": "clean non-editable wheel; hashed frozen dependencies; real stdio and HTTP",
                "production": "not deployed", "container": "not built; Docker unavailable"}
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
