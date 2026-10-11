"""Local web app on top of the runtime.

The browser never polls the runtime. Every session has one event log; the
live view is that log streamed over Server-Sent Events: first the events after
the client's `Last-Event-ID` are replayed from SQLite, then new ones are pushed
as the runtime appends them. A reconnect (or a server restart) therefore picks
up exactly where the client left off.

Runs happen in background asyncio tasks, one per session at a time. Approving
an action, sending a follow-up, branching or resuming after a restart are all
just "append an event, then run again".
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import events as ev
from ..config import AgentConfig
from ..events import Event
from ..runtime import Runtime
from ..state import replay
from ..tools import PathError, relative_parts, resolve_path
from ..util import truncate_middle

STATIC = Path(__file__).resolve().parent / "static"
MAX_EVENT_OUTPUT = 20_000
MAX_FILE_BYTES = 200_000
KEEPALIVE_S = 15.0


class SessionHub:
    """Runs sessions in the background and fans their events out to subscribers."""

    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime
        self.tasks: dict[str, asyncio.Task] = {}
        self.subscribers: dict[str, set[asyncio.Queue]] = {}
        self.errors: dict[str, str] = {}
        runtime.on_event = self.publish

    # ---------------------------------------------------------------- fan-out
    def publish(self, event: Event) -> None:
        for queue in list(self.subscribers.get(event.session_id, ())):
            queue.put_nowait(("event", event))

    def publish_status(self, session_id: str) -> None:
        payload = self.summary(session_id)
        for queue in list(self.subscribers.get(session_id, ())):
            queue.put_nowait(("status", payload))

    def subscribe(self, session_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers.setdefault(session_id, set()).add(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        self.subscribers.get(session_id, set()).discard(queue)

    def close_streams(self) -> None:
        """End every open event stream (server shutdown); browsers reconnect with Last-Event-ID."""
        for queues in self.subscribers.values():
            for queue in list(queues):
                queue.put_nowait(("close", None))

    # ------------------------------------------------------------------- runs
    def running(self, session_id: str) -> bool:
        task = self.tasks.get(session_id)
        return task is not None and not task.done()

    def start(self, session_id: str) -> bool:
        if self.running(session_id):
            return False
        self.errors.pop(session_id, None)
        self.tasks[session_id] = asyncio.create_task(self._run(session_id))
        self.publish_status(session_id)
        return True

    async def _run(self, session_id: str) -> None:
        try:
            await self.rt.run(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surfaced in the UI; the log is intact, so Resume works
            self.errors[session_id] = f"{type(exc).__name__}: {exc}"
        finally:
            asyncio.get_running_loop().call_soon(self.publish_status, session_id)

    async def stop(self, session_id: str) -> bool:
        task = self.tasks.get(session_id)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.publish_status(session_id)
        return True

    # ---------------------------------------------------------------- reading
    def summary(self, session_id: str) -> dict[str, Any]:
        events = self.rt.store.load(session_id)
        if not events:
            raise KeyError(session_id)
        state = replay(events)
        cfg = state.config
        steps = []
        for e in events:
            if e.type == ev.MODEL_RESPONSE:
                u = e.data.get("usage") or {}
                steps.append({"seq": e.seq, "input": u.get("input_tokens", 0), "cached": u.get("cached_tokens", 0),
                              "output": u.get("output_tokens", 0), "latency_ms": e.data.get("latency_ms", 0)})
        return {
            "id": session_id,
            "task": state.task,
            "status": state.status,
            "final": dict(state.final) if state.final else None,
            "running": self.running(session_id),
            "error": self.errors.get(session_id),
            "awaiting": state.awaiting,
            "step": state.step,
            "usage": asdict(state.usage),
            "taint": sorted(state.taint),
            "workspace": state.workspace,
            "parent": dict(state.parent) if state.parent else None,
            "config": {"model": cfg.model, "sandbox": cfg.sandbox, "policy": cfg.policy, "approval": cfg.approval,
                       "context_strategy": cfg.context_strategy, "max_steps": cfg.max_steps},
            "steps": steps,
            "next_seq": state.next_seq,
        }


def event_payload(event: Event) -> dict[str, Any]:
    data = dict(event.data)
    for key in ("output", "preview"):
        if isinstance(data.get(key), str) and len(data[key]) > MAX_EVENT_OUTPUT:
            data[key] = truncate_middle(data[key], MAX_EVENT_OUTPUT)
            data[f"{key}_truncated"] = True
    if event.type == ev.MODEL_RESPONSE:
        data.pop("request", None)  # digests are for `spool verify`, not for people
        data.get("message", {}).pop("provider_content", None)
    return {"seq": event.seq, "type": event.type, "ts": event.ts, "data": data}


def _sse(kind: str, payload: dict[str, Any], event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def create_app(runtime: Runtime, *, default_config: AgentConfig | None = None, read_only: bool = False) -> Starlette:
    """`read_only` serves the sessions for browsing (e.g. a run's recorded evals) and refuses every write."""
    hub = SessionHub(runtime)
    store = runtime.store
    base_config = default_config or AgentConfig()

    async def index(request: Request) -> Response:
        return FileResponse(STATIC / "index.html")

    async def server_config(request: Request) -> Response:
        return JSONResponse({"read_only": read_only, "model": base_config.model})

    def refuse_if_read_only() -> Response | None:
        if read_only:
            return JSONResponse({"error": "This server is read-only; start it without --read-only to run tasks."},
                                status_code=403)
        return None

    async def list_sessions(request: Request) -> Response:
        rows = store.sessions(limit=int(request.query_params.get("limit", 50)))
        for row in rows:
            row["running"] = hub.running(row["id"])
        return JSONResponse([r for r in rows if "." not in r["id"]])  # sub-agent sessions show inside their parent

    async def create_session(request: Request) -> Response:
        if (refused := refuse_if_read_only()) is not None:
            return refused
        body = await request.json()
        task = (body.get("task") or "").strip()
        if not task:
            return JSONResponse({"error": "Describe the task first."}, status_code=400)
        overrides = {k: body[k] for k in ("approval", "context_strategy", "max_steps", "policy") if body.get(k)}
        try:
            config = base_config.replace(**overrides)
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        workspace = body.get("workspace") or None
        if workspace:
            workspace = os.path.expanduser(workspace)
        sid = runtime.start(task, config=config, workspace=workspace)
        hub.start(sid)
        return JSONResponse(hub.summary(sid), status_code=201)

    def _session_or_404(request: Request) -> str:
        sid = request.path_params["sid"]
        if not store.exists(sid):
            raise KeyError(sid)
        return sid

    async def get_session(request: Request) -> Response:
        try:
            return JSONResponse(hub.summary(_session_or_404(request)))
        except KeyError:
            return JSONResponse({"error": "No such session."}, status_code=404)

    async def stream_events(request: Request) -> Response:
        try:
            sid = _session_or_404(request)
        except KeyError:
            return JSONResponse({"error": "No such session."}, status_code=404)
        after = request.headers.get("last-event-id") or request.query_params.get("after") or "-1"
        try:
            last = int(after)
        except ValueError:
            last = -1
        follow = request.query_params.get("follow", "1") != "0"

        async def stream() -> AsyncIterator[str]:
            queue = hub.subscribe(sid)  # subscribe first, so nothing appended during the replay is missed
            seen = last
            try:
                for event in store.load(sid):
                    if event.seq > seen:
                        yield _sse("event", event_payload(event), event.seq)
                        seen = event.seq
                yield _sse("status", hub.summary(sid))
                while follow:
                    try:
                        kind, item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield ": keep-alive\n\n"
                        continue
                    if kind == "close":
                        break
                    if kind == "event" and item.seq > seen:
                        yield _sse("event", event_payload(item), item.seq)
                        seen = item.seq
                    elif kind == "status":
                        yield _sse("status", item)
            finally:
                hub.unsubscribe(sid, queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def post_message(request: Request) -> Response:
        if (refused := refuse_if_read_only()) is not None:
            return refused
        try:
            sid = _session_or_404(request)
        except KeyError:
            return JSONResponse({"error": "No such session."}, status_code=404)
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "Write a message first."}, status_code=400)
        if hub.running(sid):
            return JSONResponse({"error": "The agent is still working; wait for it to finish or stop it."}, status_code=409)
        runtime.send(sid, text)
        hub.start(sid)
        return JSONResponse(hub.summary(sid))

    async def post_approval(request: Request) -> Response:
        if (refused := refuse_if_read_only()) is not None:
            return refused
        try:
            sid = _session_or_404(request)
        except KeyError:
            return JSONResponse({"error": "No such session."}, status_code=404)
        body = await request.json()
        try:
            runtime.approve(sid, body["call_id"], bool(body.get("approved")), by=body.get("by") or "web",
                            note=body.get("note") or "")
        except (KeyError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        hub.start(sid)
        return JSONResponse(hub.summary(sid))

    async def post_resume(request: Request) -> Response:
        if (refused := refuse_if_read_only()) is not None:
            return refused
        try:
            sid = _session_or_404(request)
        except KeyError:
            return JSONResponse({"error": "No such session."}, status_code=404)
        hub.start(sid)
        return JSONResponse(hub.summary(sid))

    async def post_stop(request: Request) -> Response:
        if (refused := refuse_if_read_only()) is not None:
            return refused
        try:
            sid = _session_or_404(request)
        except KeyError:
            return JSONResponse({"error": "No such session."}, status_code=404)
        stopped = await hub.stop(sid)
        return JSONResponse({**hub.summary(sid), "stopped": stopped})

    async def post_fork(request: Request) -> Response:
        if (refused := refuse_if_read_only()) is not None:
            return refused
        try:
            sid = _session_or_404(request)
        except KeyError:
            return JSONResponse({"error": "No such session."}, status_code=404)
        body = await request.json()
        try:
            new_sid = runtime.fork(sid, int(body["at_seq"]))
        except (KeyError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if body.get("run", True):
            hub.start(new_sid)
        return JSONResponse(hub.summary(new_sid), status_code=201)

    async def files(request: Request) -> Response:
        try:
            sid = _session_or_404(request)
        except KeyError:
            return JSONResponse({"error": "No such session."}, status_code=404)
        workspace = Path(hub.summary(sid)["workspace"])
        rel = request.query_params.get("path") or "."
        try:
            target = resolve_path(workspace, rel)
        except PathError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        parts = relative_parts(workspace, target)
        protected = runtime.state(sid).config.sensitive_paths
        if parts and parts[0] in protected:
            return JSONResponse({"error": "This folder is protected; its contents are not shown here."}, status_code=403)
        if target.is_dir():
            entries = []
            for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
                if child.name in (".git", "__pycache__", ".pytest_cache"):
                    continue
                entries.append({"name": child.name, "dir": child.is_dir(),
                                "size": child.stat().st_size if child.is_file() else None,
                                "path": str(Path(os.path.relpath(child, os.path.realpath(workspace))))})
            return JSONResponse({"path": rel, "entries": entries})
        if not target.exists():
            return JSONResponse({"error": f"{rel} does not exist."}, status_code=404)
        raw = target.read_bytes()[:MAX_FILE_BYTES]
        if b"\x00" in raw[:4096]:
            return JSONResponse({"path": rel, "binary": True, "size": target.stat().st_size})
        return JSONResponse({"path": rel, "content": raw.decode("utf-8", "replace"),
                             "truncated": target.stat().st_size > MAX_FILE_BYTES, "size": target.stat().st_size})

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        hub.close_streams()
        for sid in list(hub.tasks):
            await hub.stop(sid)
        await runtime.aclose()

    routes = [
        Route("/", index),
        Route("/api/config", server_config, methods=["GET"]),
        Route("/api/sessions", list_sessions, methods=["GET"]),
        Route("/api/sessions", create_session, methods=["POST"]),
        Route("/api/sessions/{sid}", get_session, methods=["GET"]),
        Route("/api/sessions/{sid}/events", stream_events, methods=["GET"]),
        Route("/api/sessions/{sid}/messages", post_message, methods=["POST"]),
        Route("/api/sessions/{sid}/approvals", post_approval, methods=["POST"]),
        Route("/api/sessions/{sid}/resume", post_resume, methods=["POST"]),
        Route("/api/sessions/{sid}/stop", post_stop, methods=["POST"]),
        Route("/api/sessions/{sid}/fork", post_fork, methods=["POST"]),
        Route("/api/sessions/{sid}/files", files, methods=["GET"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.hub = hub
    return app
