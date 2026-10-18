"""Parse the Open GoPro media list (GET /gopro/media/list)."""

from __future__ import annotations

from typing import Any

from gopro_sync.cameras.base import RemoteMedia


def _expand_group(name: str, item: dict[str, Any]) -> list[str]:
    """Grouped items (bursts, time lapse photos) are listed once, e.g. G0010001.JPG with b=1, l=30.

    Member files share the 4 character prefix and use a 4 digit running id.
    """
    stem, _, ext = name.rpartition(".")
    if len(stem) < 8 or "b" not in item or "l" not in item:
        return [name]
    prefix = stem[:4]
    missing = {int(m) for m in item.get("m", [])}
    first, last = int(item["b"]), int(item["l"])
    return [f"{prefix}{i:04d}.{ext}" for i in range(first, last + 1) if i not in missing]


def parse_media_list(data: dict[str, Any]) -> list[RemoteMedia]:
    result: list[RemoteMedia] = []
    for directory in data.get("media", []):
        folder = directory["d"]
        for item in directory.get("fs", []):
            created = int(item.get("cre") or item.get("mod") or 0)
            if "g" in item:
                # For groups "s" is the member count, not a byte size
                for member in _expand_group(item["n"], item):
                    result.append(RemoteMedia(folder=folder, filename=member, created=created, size=None))
            else:
                size = int(item["s"]) if "s" in item else None
                result.append(RemoteMedia(folder=folder, filename=item["n"], created=created, size=size))
    return result
