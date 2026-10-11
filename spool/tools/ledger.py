"""Append-only ledger for external side effects, deduplicated by idempotency key.

A tool with an external effect (sending a message, posting to an API) writes
its effect here under the key `<session>:<call_id>`. If a crash lands between
"effect happened" and "TOOL_COMPLETED appended", the resumed runtime calls the
tool again with the same key and gets the recorded result back instead of a
second effect. Check-and-append runs under an exclusive file lock, and the
record is fsynced before the tool returns.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, key: str, entry: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Append `entry` under `key` unless the key exists. Returns (stored_entry, duplicate)."""
        with open(self.path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                for line in fh:
                    if line.strip():
                        existing = json.loads(line)
                        if existing.get("key") == key:
                            return existing, True
                stored = {"key": key, **entry}
                fh.write(json.dumps(stored, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
                return stored, False
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
