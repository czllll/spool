"""Replay checks: is the recorded history reproducible from the log alone?

`verify_render` rebuilds the state before every MODEL_RESPONSE, re-renders the
request with today's code and compares its per-part digests with the ones
recorded at the time. Zero mismatches means the context engine is a pure
function of the log: what the model saw can be reconstructed exactly, forks
render the same prefix, and caches are not broken by hidden inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from . import events as ev
from .context import render
from .events import Event
from .state import State, reduce
from .tools import ToolRegistry
from .tools.builtin import DEFAULT_TOOLS, builtin_registry


@dataclass
class RenderCheck:
    checked: int = 0
    mismatches: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches


def verify_render(events: Sequence[Event], tools: ToolRegistry | None = None) -> RenderCheck:
    tools = tools or builtin_registry()
    result = RenderCheck()
    state = State()
    for i, e in enumerate(events):
        if e.type == ev.MODEL_RESPONSE and e.data.get("request"):
            cfg = state.config
            schemas = tools.subset(cfg.tools if cfg.tools is not None else DEFAULT_TOOLS).schemas()
            request = render(events[:i], state, schemas, now=e.data.get("rendered_at", e.ts))
            got = request.digest_parts()
            want = e.data["request"]
            result.checked += 1
            if got != want:
                result.mismatches.append({"seq": e.seq, "first_difference": _first_difference(want, got)})
        state = reduce(state, e)
    return result


def _first_difference(want: dict, got: dict) -> str:
    for key in ("system", "tools"):
        if want.get(key) != got.get(key):
            return key
    w, g = want.get("messages", []), got.get("messages", [])
    for i, (a, b) in enumerate(zip(w, g)):
        if a != b:
            return f"message {i}"
    if len(w) != len(g):
        return f"message count {len(w)} vs {len(g)}"
    return "digest"
