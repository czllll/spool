"""Self-contained HTML timeline of a session (no external assets).

Shows the task and config, a per-step bar of prompt tokens split into
reused-from-cache and recomputed, and every event in order. Tool outputs are
collapsible; each step carries the `spool fork` command that branches there.
"""

from __future__ import annotations

import html
import json
from typing import Mapping, Sequence

from . import events as ev
from .events import Event
from .state import replay
from .util import iso

CSS = """
:root{--bg:#fbfaf7;--fg:#1f2328;--muted:#656d76;--card:#ffffff;--line:#e4e2dc;--accent:#3b6fd8;--ok:#2f8f4e;
--bad:#c4432b;--warn:#b7791f;--cache:#7aa7f0;--fresh:#e8a33d;--code:#f4f2ed}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e6e6e6;--muted:#9aa1aa;--card:#1e2126;--line:#2e3239;
--accent:#7aa2ff;--ok:#5cc27c;--bad:#ff7a63;--warn:#e0b050;--cache:#4f7cc9;--fresh:#c98a2c;--code:#262a30}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,
"Segoe UI",Helvetica,Arial,sans-serif}main{max-width:980px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:20px;margin:0 0 4px}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:8px;margin:16px 0}.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px}
.stat b{display:block;font-size:18px}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:8px 0}
.tag{display:inline-block;font-size:11px;padding:1px 6px;border-radius:10px;border:1px solid var(--line);color:var(--muted);margin-right:6px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}pre{background:var(--code);padding:8px;border-radius:6px;
overflow:auto;max-height:420px;white-space:pre-wrap;word-break:break-word;margin:6px 0 0;font-size:12.5px}
details summary{cursor:pointer;color:var(--muted)}.step{margin-top:20px;border-top:1px solid var(--line);padding-top:8px}
.bars{display:flex;align-items:flex-end;gap:3px;height:90px;margin:8px 0 2px}.bar{flex:1;display:flex;flex-direction:column-reverse;min-width:4px}
.bar span{display:block}.legend span{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 12px;vertical-align:-1px}
code{background:var(--code);padding:1px 4px;border-radius:4px;font-size:12.5px}
"""


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _json(obj: object) -> str:
    return _e(json.dumps(obj, ensure_ascii=False, indent=2))


def _token_bars(events: Sequence[Event]) -> str:
    resp = [e for e in events if e.type == ev.MODEL_RESPONSE]
    if not resp:
        return ""
    peak = max((e.data.get("usage") or {}).get("input_tokens", 0) for e in resp) or 1
    bars = []
    for i, e in enumerate(resp, start=1):
        u = e.data.get("usage") or {}
        total = u.get("input_tokens", 0)
        cached = min(u.get("cached_tokens", 0), total)
        fresh = total - cached
        bars.append(
            f'<div class="bar" title="step {i}: {total} prompt tokens, {cached} reused from cache, {fresh} recomputed">'
            f'<span style="height:{90 * cached / peak:.1f}px;background:var(--cache)"></span>'
            f'<span style="height:{90 * fresh / peak:.1f}px;background:var(--fresh)"></span></div>'
        )
    return (
        '<div class="card"><b>Prompt tokens per step</b>'
        '<span class="legend muted"><span style="background:var(--cache)"></span>reused from cache'
        '<span style="background:var(--fresh)"></span>recomputed</span>'
        f'<div class="bars">{"".join(bars)}</div><div class="muted">steps 1–{len(resp)}; '
        f"peak {peak:,} tokens</div></div>"
    )


def _event_html(e: Event, sid: str) -> str:
    d = e.data
    t = e.type
    if t == ev.USER_MESSAGE:
        return f'<div class="card"><span class="tag">user</span><pre>{_e(d["text"])}</pre></div>'
    if t == ev.MODEL_RESPONSE:
        msg = d["message"]
        u = d.get("usage") or {}
        calls = "".join(
            f'<div><code>{_e(c["name"])}</code><pre>{_json(c.get("arguments") or c.get("raw_arguments"))}</pre></div>'
            for c in msg.get("tool_calls") or []
        )
        text = f"<pre>{_e(msg['content'])}</pre>" if msg.get("content") else ""
        meta = (f'{u.get("input_tokens", 0):,} in · {u.get("cached_tokens", 0):,} cached · '
                f'{u.get("output_tokens", 0):,} out · {d.get("latency_ms", 0) / 1000:.1f}s')
        return (f'<div class="card"><span class="tag">assistant</span><span class="muted">{meta}</span>'
                f'{text}{calls}</div>')
    if t == ev.POLICY_DECISION:
        cls = "ok" if d["verdict"] == "allow" else "bad"
        props = ", ".join(d.get("properties") or []) or "no risk properties"
        reason = f"<div class='muted'>{_e(d.get('reason'))}</div>" if d.get("reason") else ""
        return (f'<div class="card"><span class="tag">policy</span><b class="{cls}">{_e(d["verdict"])}</b> '
                f'{_e(d["tool"])} <span class="muted">({_e(d.get("rule"))}; {props})</span>{reason}</div>')
    if t == ev.TOOL_COMPLETED:
        cls = "ok" if d.get("ok") else "bad"
        label = "ok" if d.get("ok") else f'{d.get("kind")}: {d.get("error")}'
        body = d.get("output") or ""
        extra = ""
        if d.get("offloaded_path"):
            extra += f'<div class="muted">offloaded to <code>{_e(d["offloaded_path"])}</code> ({d["full_chars"]:,} chars)</div>'
        for key in ("loop_warning", "reminder"):
            if d.get(key):
                extra += f'<pre class="warn">{_e(d[key])}</pre>'
        return (f'<div class="card"><span class="tag">tool</span><code>{_e(d.get("name"))}</code> '
                f'<b class="{cls}">{_e(label)[:300]}</b> <span class="muted">{d.get("duration_ms", "")} ms</span>'
                f'<details><summary>output ({len(body):,} chars)</summary><pre>{_e(body)}</pre></details>{extra}</div>')
    if t in (ev.APPROVAL_REQUESTED, ev.APPROVAL_DECIDED, ev.CONTEXT_FOLDED, ev.CONTEXT_COMPACTED, ev.MODEL_ERROR,
             ev.NOTE, ev.SESSION_FINISHED):
        return f'<div class="card"><span class="tag">{_e(t)}</span><pre>{_json(d)}</pre></div>'
    return ""


def render_html(events: Sequence[Event], children: Mapping[str, Sequence[Event]] | None = None) -> str:
    state = replay(events)
    sid = state.session_id
    cfg = events[0].data.get("config", {})
    final = state.final or {}
    u = state.usage
    reuse = (u.cached_tokens / u.input_tokens) if u.input_tokens else 0.0
    stats = [
        ("status", final.get("status", state.status)),
        ("steps", state.step),
        ("tool calls", f"{state.tool_calls} ({state.tool_errors} errors)"),
        ("prompt tokens", f"{u.input_tokens:,}"),
        ("cache reuse", f"{100 * reuse:.0f}%"),
        ("output tokens", f"{u.output_tokens:,}"),
        ("model time", f"{u.model_latency_ms / 1000:.0f}s"),
        ("taint", ", ".join(sorted(state.taint)) or "none"),
    ]
    parts = [
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Spool session {_e(sid)}</title><style>{CSS}</style></head><body><main>",
        f"<h1>{_e(state.task[:160])}</h1>",
        f"<div class='muted'>session <code>{_e(sid)}</code> · started {iso(events[0].ts)} · model <code>{_e(cfg.get('model'))}</code>"
        f" · context <code>{_e(cfg.get('context_strategy'))}</code> · policy <code>{_e(cfg.get('policy'))}/{_e(cfg.get('approval'))}</code>"
        f" · sandbox <code>{_e(cfg.get('sandbox'))}</code></div>",
        "<div class='grid'>" + "".join(f"<div class='stat'><span class='muted'>{_e(k)}</span><b>{_e(v)}</b></div>" for k, v in stats) + "</div>",
        _token_bars(events),
    ]
    if final.get("answer"):
        parts.append(f"<div class='card'><b>Final answer</b><pre>{_e(final['answer'])}</pre></div>")
    step = 0
    for e in events[1:]:
        if e.type == ev.MODEL_RESPONSE:
            step += 1
            parts.append(f"<div class='step'><b>Step {step}</b> <span class='muted'>· event {e.seq} · "
                         f"branch here: <code>spool fork {_e(sid)} --at {e.seq - 1}</code></span></div>")
        if e.type in (ev.TOOL_STARTED,):
            continue
        parts.append(_event_html(e, sid))
    for child_id, child_events in (children or {}).items():
        cstate = replay(child_events)
        parts.append(f"<div class='step'><b>Sub-agent {_e(child_id)}</b> <span class='muted'>{cstate.step} steps · "
                     f"{_e((cstate.final or {}).get('status', cstate.status))}</span></div>")
        parts.extend(_event_html(e, child_id) for e in child_events[1:] if e.type != ev.TOOL_STARTED)
    parts.append("</main></body></html>")
    return "\n".join(parts)
