"""Export eval trajectories as chat-format JSONL for fine-tuning.

Each line is one trial: the system prompt, the conversation exactly as the
model saw it at its last step (tool calls and results included), the tool
schemas, and the outcome. With `passed_only` (default) this is the data set
for rejection-sampling fine-tuning: keep the trajectories the verifier
accepted and train on them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from . import events as ev
from .connectors import session_registry
from .context import DEFAULT_SYSTEM_PROMPT, build_messages, memory_block, services_block, skills_block
from .eval.runner import load_results
from .models.openai_compat import OpenAICompatClient
from .state import replay
from .store import EventStore
from .tools.builtin import builtin_registry


def trajectory(events: list) -> dict:
    state = replay(events)
    cfg = state.config
    tools = session_registry(state, builtin_registry()).schemas()
    # Final assistant turn included: build from the whole log, not just up to the last request.
    wire = [OpenAICompatClient._wire_message(m) for m in build_messages(events, state)]
    return {
        "messages": [{"role": "system", "content": (cfg.system_prompt or DEFAULT_SYSTEM_PROMPT) + services_block(state)
                      + skills_block(state) + memory_block(state)},
                     *wire],
        "tools": [{"type": "function", "function": t} for t in tools],
        "meta": {
            "session_id": state.session_id,
            "status": (state.final or {}).get("status"),
            "steps": state.step,
            "folded_or_compacted": any(e.type in (ev.CONTEXT_FOLDED, ev.CONTEXT_COMPACTED) for e in events),
        },
    }


def export_results(result_dirs: Iterable[str | Path], output: Path, *, passed_only: bool = True) -> int:
    count = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as fh:
        for directory in result_dirs:
            directory = Path(directory)
            store = EventStore(directory / "spool.db")
            for r in load_results(directory / "results.jsonl"):
                if passed_only and not r.get("passed"):
                    continue
                events = store.load(r["session_id"])
                if not events:
                    continue
                item = trajectory(events)
                item["meta"].update(task_id=r["task_id"], trial=r["trial"], config=r["config"], passed=r["passed"])
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                count += 1
            store.close()
    return count
