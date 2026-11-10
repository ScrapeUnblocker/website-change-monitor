"""What each page looked like when it was last delivered, kept in ``state/``.

``state/pages.json`` holds one entry per page: fingerprint, size, when it was first seen and
last changed, how many changes were delivered, and a pending or failure counter when one
applies. ``state/pages/<id>.md`` holds the compared text itself, so the repo's git history
doubles as the history of every page: ``git log -p state/pages/github.com-pricing.md``.

Nothing in here changes on a run where no page changed, so the workflow only commits when
there is news.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

STATE_VERSION = 1


class StateError(ValueError):
    """The state folder exists but cannot be read."""


class Store:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @property
    def index_file(self) -> Path:
        return self.root / "pages.json"

    def snapshot_file(self, page_id: str) -> Path:
        return self.root / "pages" / f"{page_id}.md"

    def load(self) -> dict[str, dict[str, Any]]:
        """``{page_id: entry}``; empty when there is no state yet."""
        path = self.index_file
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"cannot read state file {path}: {exc}") from None
        pages = data.get("pages") if isinstance(data, dict) else None
        if not isinstance(pages, dict) or not all(isinstance(v, dict) for v in pages.values()):
            raise StateError(f"state file {path} has no 'pages' mapping")
        return pages

    def read_snapshot(self, page_id: str) -> list[str] | None:
        path = self.snapshot_file(page_id)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise StateError(f"cannot read {path}: {exc}") from None

    def save(
        self,
        pages: dict[str, dict[str, Any]],
        snapshots: dict[str, list[str]] | None = None,
    ) -> list[Path]:
        """Write the index and any new snapshots. Returns the files that changed."""
        changed: list[Path] = []
        for page_id, lines in sorted((snapshots or {}).items()):
            text = "\n".join(lines) + "\n"
            if _write_if_changed(self.snapshot_file(page_id), text):
                changed.append(self.snapshot_file(page_id))
        # Sorted keys keep the file byte-identical when nothing changed.
        payload = {"pages": pages, "version": STATE_VERSION}
        text = json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
        if _write_if_changed(self.index_file, text):
            changed.append(self.index_file)
        return changed


def _write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return True
