"""Operator-owned outgoing folders and bounded downloads; no client roots are trusted."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from zai_telegram.secrets import require_private_file
from zai_telegram.transport import ProviderError

FILE_FIELDS = {
    "send_file": "file_path",
    "send_album": "file_paths",
    "send_voice": "file_path",
    "upload_file": "file_path",
    "send_sticker": "file_path",
    "set_profile_photo": "file_path",
    "edit_chat_photo": "file_path",
}
EXTENSIONS = {
    "send_voice": {".ogg", ".opus"},
    "send_sticker": {".webp"},
    "set_profile_photo": {".jpg", ".jpeg", ".png", ".webp"},
    "edit_chat_photo": {".jpg", ".jpeg", ".png", ".webp"},
}


@dataclass(frozen=True)
class MediaPolicy:
    roots: tuple[Path, ...] = ()
    download_root: Path | None = None
    max_file_bytes: int = 20 * 1024 * 1024

    @classmethod
    def load(cls, filename, *, protected=()):
        if not filename:
            return cls()
        path = Path(filename).resolve(strict=True)
        require_private_file(path)
        if path.stat().st_size > 16384:
            raise ValueError("media policy is too large")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) - {"roots", "download_root", "max_file_bytes"}:
            raise ValueError("invalid media policy fields")
        values = data.get("roots", [])
        if not isinstance(values, list) or len(values) > 8:
            raise ValueError("provide at most eight outgoing folders")

        def folder(value):
            if not isinstance(value, str) or not value or len(value) > 2048:
                raise ValueError("bounded media folder required")
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = path.parent / candidate
            resolved = candidate.resolve(strict=True)
            if not resolved.is_dir() or resolved == Path(resolved.anchor):
                raise ValueError("media folder must be an existing dedicated directory")
            for item in (path, *protected):
                if item and Path(item).resolve().is_relative_to(resolved):
                    raise ValueError("media folder contains a protected configuration or state file")
            return resolved

        roots = tuple(dict.fromkeys(folder(value) for value in values))
        download = folder(data["download_root"]) if data.get("download_root") else None
        if download and any(download.is_relative_to(root) or root.is_relative_to(download) for root in roots):
            raise ValueError("incoming and outgoing folders must be separate")
        maximum = data.get("max_file_bytes", 20 * 1024 * 1024)
        if type(maximum) is not int or not 1 <= maximum <= 50 * 1024 * 1024:
            raise ValueError("file limit must be between 1 byte and 50 MiB")
        return cls(roots, download, maximum)

    def fingerprint(self):
        return [list(map(str, self.roots)), str(self.download_root), self.max_file_bytes]

    def validate(self, tool, arguments):
        if tool not in FILE_FIELDS and tool != "download_media":
            return arguments
        if any(key in arguments for key in ("ctx", "roots", "allowed_roots", "media_policy")):
            raise ProviderError("media permissions belong to the operator")
        values = dict(arguments)
        if tool == "download_media":
            if self.download_root is None or values.get("file_path") is not None:
                raise ProviderError("downloads require an operator folder; caller paths are not supported")
            if type(values.get("message_id")) is not int or not 1 <= values["message_id"] <= 2147483647:
                raise ProviderError("positive message ID required")
        else:
            field = FILE_FIELDS[tool]
            raw = values.get(field)
            multiple = isinstance(raw, list)
            if (tool == "send_album" and not multiple) or (
                multiple and tool not in {"send_file", "send_album"}
            ):
                raise ProviderError("invalid media file list")
            paths = raw if multiple else [raw]
            if multiple and not 2 <= len(paths) <= 10:
                raise ProviderError("albums require 2 to 10 files")
            normalized, total = [], 0
            for value in paths:
                candidate = self.readable(value, tool)
                total += candidate.stat().st_size
                normalized.append(str(candidate))
            if total > self.max_file_bytes:
                raise ProviderError("total media size exceeds the operator limit")
            values[field] = normalized if multiple else normalized[0]
        if tool not in {"upload_file", "set_profile_photo"}:
            chat = values.get("chat_id")
            if (
                isinstance(chat, bool)
                or not isinstance(chat, (str, int))
                or not re.fullmatch(r"-?[1-9][0-9]{0,18}", str(chat))
            ):
                raise ProviderError("media requires an explicit numeric chat ID")
        return values

    def readable(self, value, tool):
        if not self.roots or not isinstance(value, str) or not 1 <= len(value) <= 2048:
            raise ProviderError("outgoing files require an operator media folder")
        if any(char in value for char in ("\x00", "*", "?", "[", "]", "{", "}", "~")):
            raise ProviderError("invalid media path")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.roots[0] / candidate
        # Symlinks/junctions and hard links are not useful in an outgoing staging folder.
        for part in (candidate, *candidate.parents):
            if part.is_symlink() or part.is_junction():
                raise ProviderError("linked media paths are not supported")
        candidate = candidate.resolve(strict=True)
        info = candidate.stat()
        if not any(candidate.is_relative_to(root) for root in self.roots):
            raise ProviderError("media path is outside operator folders")
        maximum = (
            min(self.max_file_bytes, 10 * 1024 * 1024) if tool == "send_sticker" else self.max_file_bytes
        )
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= maximum:
            raise ProviderError("media must be a nonempty regular file within the size limit")
        if tool in EXTENSIONS and candidate.suffix.lower() not in EXTENSIONS[tool]:
            raise ProviderError("unsupported media extension")
        return candidate


class BoundedWriter:
    def __init__(self, file, maximum):
        self.file, self.maximum, self.size = file, maximum, 0

    def write(self, data):
        if self.size + len(data) > self.maximum:
            raise ProviderError("download exceeds the operator limit")
        count = self.file.write(data)
        self.size += count
        return count


async def exact_chat(runtime, client, identifier):
    from telethon.utils import get_peer_id

    entity = await runtime.resolve_entity(int(identifier), client)
    if get_peer_id(entity) != int(identifier):
        raise ProviderError("resolved chat differs from the explicit ID; use its marked Telegram ID")
    return entity


def verify_receipt(tool, value):
    """Pinned file tools sometimes return refusals as ordinary strings."""
    if tool not in FILE_FIELDS:
        return
    if tool == "upload_file":
        try:
            record = json.loads(value) if isinstance(value, str) else value
        except (ValueError, TypeError):
            record = None
        if isinstance(record, dict) and isinstance(record.get("path"), str) and "md5_checksum" in record:
            return
    else:
        prefixes = {
            "send_file": ("File sent to chat ", "Album sent to chat "),
            "send_album": ("Album sent to chat ",),
            "send_voice": ("Voice message sent to chat ",),
            "send_sticker": ("Sticker sent to chat ",),
            "set_profile_photo": ("Profile photo updated from ",),
            "edit_chat_photo": ("Chat ",),
        }
        if (
            isinstance(value, str)
            and value.startswith(prefixes[tool])
            and (tool != "edit_chat_photo" or " photo updated from " in value)
        ):
            return
    raise ProviderError("embedded media tool did not confirm completion; inspect the outbox")


async def download(policy, runtime, arguments):
    client = runtime.get_client(arguments["account"])
    entity = await exact_chat(runtime, client, arguments["chat_id"])
    message = await client.get_messages(entity, ids=arguments["message_id"])
    size = getattr(getattr(message, "file", None), "size", None)
    if not getattr(message, "media", None) or type(size) is not int or not 0 < size <= policy.max_file_bytes:
        raise ProviderError("media is missing or its declared size is unsupported")
    root = policy.download_root.resolve(strict=True)
    if root != policy.download_root or not root.is_dir():
        raise ProviderError("download folder changed; restart with valid policy")
    destination = root / ("telegram-" + uuid4().hex + ".bin")
    completed, created = False, False
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as file:
            writer = BoundedWriter(file, min(size, policy.max_file_bytes))
            await client.download_media(message, file=writer)
            if writer.size != size:
                raise ProviderError("download size differs from provider metadata")
        completed = True
        return {
            "status": "downloaded",
            "path": str(destination),
            "bytes": size,
            "message_id": arguments["message_id"],
            "untrusted_content": True,
        }
    finally:
        if created and not completed:
            destination.unlink(missing_ok=True)
