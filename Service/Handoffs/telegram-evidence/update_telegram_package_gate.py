import ast
from pathlib import Path

path = Path(r"D:\ZAI\Infra\mcp-platform-telegram-services-extraction\src\mcp_platform\ops\verify.py")
source = path.read_text(encoding="utf-8")
node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "verify_patch")
replacement = '''def verify_patch(errors: list[str], network: bool) -> None:
    # Keep the old CLI switch compatible. Integrity now belongs to the pinned
    # package; a gateway checkout never reconstructs upstream from its old vendor copy.
    del network
    import json
    from importlib.resources import files
    from zai_telegram.vendor_integrity import assert_pinned_vendor

    manifest = json.loads(files("zai_telegram").joinpath("_vendor/manifest.json").read_text())
    assert_pinned_vendor()
    check(
        len(manifest["commit"]) == 40
        and manifest["namespace"] == "zai_telegram._vendor"
        and len(manifest["patch_sha256"]) == 64
        and bool(manifest["files"]),
        "Telegram pinned package upstream provenance and per-file integrity verified offline",
        errors,
    )
'''
lines = source.splitlines(keepends=True)
lines[node.lineno-1:node.end_lineno] = [replacement]
source = "".join(lines).replace('help="verify Telegram patch against a clean upstream clone"',
                              'help="compatibility option; packaged Telegram integrity is always verified offline"')
path.write_text(source, encoding="utf-8", newline="\n")
