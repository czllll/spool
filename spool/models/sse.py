"""Incremental Server-Sent Events parser (WHATWG event-stream format)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SSEEvent:
    data: str
    event: str | None = None
    id: str | None = None


class SSEParser:
    """Feed lines (without the trailing newline); get an event at each blank line.

    Handles multi-line `data:` fields, comments, `event:`/`id:` fields and the
    optional single space after the colon. Unknown fields are ignored, as the
    spec requires.
    """

    def __init__(self) -> None:
        self._data: list[str] = []
        self._event: str | None = None
        self._id: str | None = None

    def feed(self, line: str) -> SSEEvent | None:
        line = line.rstrip("\r")
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        name, sep, value = line.partition(":")
        if not sep:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if name == "data":
            self._data.append(value)
        elif name == "event":
            self._event = value
        elif name == "id":
            self._id = value
        return None

    def flush(self) -> SSEEvent | None:
        return self._dispatch()

    def _dispatch(self) -> SSEEvent | None:
        if not self._data:
            self._event = None
            return None
        event = SSEEvent(data="\n".join(self._data), event=self._event, id=self._id)
        self._data = []
        self._event = None
        return event
