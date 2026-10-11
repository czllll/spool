"""Routines: saved tasks that start sessions on their own.

A routine is a task template plus triggers:
  cron      "0 9 * * 1-5"       wall-clock schedule in the routine's time zone
  interval  every N seconds
  file      new or changed files in a folder of the routine's workspace
  webhook   POST /api/routines/<name>/fire (or `spool routine fire`)

Each routine is an event stream (`routine:<name>`) in the same store as the
sessions, and the scheduler follows the same rule as the runtime: the
ROUTINE_FIRED event, which names the session it is about to start, is appended
*before* the session is started. Consequences:
  - exactly one run per scheduled time: the append carries the stream's
    expected seq, so two schedulers racing on one routine cannot both fire it;
  - a crash between "fired" and "started" is repaired on the next tick, and
    because the session id is derived from the fire, it is never started twice;
  - "why did this run?" always has an answer in the log.

`due` is pure (state, now, file snapshot) -> fires; the `Scheduler` performs
them. Missed times (the scheduler was down) follow the routine's `misfire`
policy: "run_once" runs one catch-up for the latest missed time, "skip"
records the miss and waits for the next time. `overlap="skip"` does not start
a run while the previous one is still running, sleeping or waiting for
approval.

Runs started by a routine have nobody watching them, so their approvals
expire (`approval_ttl_s`, default 10 minutes) instead of waiting forever; an
expired approval is a denial and the action does not run.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import events as ev
from .config import AgentConfig
from .cron import Cron, CronError
from .events import Event
from .store import ConcurrencyError, EventStore
from .util import digest, iso

TRIGGER_KINDS = ("cron", "interval", "file", "webhook")
OVERLAP = ("skip", "allow")
MISFIRE = ("run_once", "skip")
UNFINISHED = ("running", "awaiting_approval", "sleeping")
DEFAULT_APPROVAL_TTL_S = 600.0
MAX_FILES_PER_FIRE = 200
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


class RoutineError(ValueError):
    pass


# --------------------------------------------------------------------- spec
@dataclass(frozen=True)
class Trigger:
    kind: str
    cron: str | None = None
    every_s: float | None = None
    path: str | None = None   # file trigger: folder relative to the routine workspace
    pattern: str = "*"        # file trigger: glob on file names

    def validate(self) -> None:
        if self.kind not in TRIGGER_KINDS:
            raise RoutineError(f"trigger kind must be one of {TRIGGER_KINDS}, got {self.kind!r}")
        if self.kind == "cron":
            if not self.cron:
                raise RoutineError("a cron trigger needs an expression, e.g. '0 9 * * 1-5'")
            try:
                Cron.parse(self.cron)
            except CronError as exc:
                raise RoutineError(f"bad cron expression: {exc}") from None
        if self.kind == "interval" and not (self.every_s and self.every_s >= 60):
            raise RoutineError("an interval trigger needs every_s >= 60")
        if self.kind == "file":
            if not self.path or os.path.isabs(self.path) or ".." in Path(self.path).parts:
                raise RoutineError("a file trigger needs a folder inside the routine workspace, e.g. 'inbox'")

    def describe(self) -> str:
        from .cron import describe

        if self.kind == "cron":
            return describe(self.cron or "")
        if self.kind == "interval":
            s = self.every_s or 0
            return f"every {s / 3600:g} h" if s >= 3600 and s % 3600 == 0 else f"every {s / 60:g} min"
        if self.kind == "file":
            return f"new files in {self.path}/" + ("" if self.pattern == "*" else f" ({self.pattern})")
        return "webhook"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None and not (k == "pattern" and v == "*")}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Trigger":
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise RoutineError(f"unknown trigger fields: {sorted(unknown)}")
        return cls(**dict(d))


@dataclass(frozen=True)
class RoutineSpec:
    name: str
    task: str                                  # may use {date}, {time}, {now}, {files}, {payload}
    triggers: tuple[Trigger, ...] = ()
    config: Mapping[str, Any] = field(default_factory=dict)  # AgentConfig overrides for its runs
    workspace: str | None = None               # None: <data_dir>/routines/<name>, kept across runs
    timezone: str | None = None                # IANA name for cron triggers; None: the machine's zone
    overlap: str = "skip"
    misfire: str = "run_once"
    approval_ttl_s: float | None = DEFAULT_APPROVAL_TTL_S

    def validate(self) -> None:
        if not NAME_RE.match(self.name):
            raise RoutineError("routine names use lowercase letters, digits and dashes (max 48), e.g. 'morning-inbox'")
        if not self.task.strip():
            raise RoutineError("a routine needs a task")
        for trigger in self.triggers:
            trigger.validate()
        if self.overlap not in OVERLAP:
            raise RoutineError(f"overlap must be one of {OVERLAP}")
        if self.misfire not in MISFIRE:
            raise RoutineError(f"misfire must be one of {MISFIRE}")
        if self.timezone:
            try:
                ZoneInfo(self.timezone)
            except Exception:
                raise RoutineError(f"unknown time zone {self.timezone!r}") from None
        try:
            AgentConfig.from_dict(dict(self.config))
        except (TypeError, ValueError) as exc:
            raise RoutineError(f"bad config: {exc}") from None
        if self.approval_ttl_s is not None and self.approval_ttl_s <= 0:
            raise RoutineError("approval_ttl_s must be positive")

    def tz(self) -> tzinfo:
        if self.timezone:
            return ZoneInfo(self.timezone)
        return datetime.now().astimezone().tzinfo or timezone.utc

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "task": self.task, "triggers": [t.to_dict() for t in self.triggers],
            "config": dict(self.config), "workspace": self.workspace, "timezone": self.timezone,
            "overlap": self.overlap, "misfire": self.misfire, "approval_ttl_s": self.approval_ttl_s,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], *, validate: bool = True) -> "RoutineSpec":
        d = dict(d)
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise RoutineError(f"unknown routine fields: {sorted(unknown)}")
        d["triggers"] = tuple(Trigger.from_dict(t) for t in d.get("triggers") or ())
        d["config"] = dict(d.get("config") or {})
        spec = cls(**d)
        if validate:  # replaying a recorded spec must not depend on today's validation rules
            spec.validate()
        return spec


# -------------------------------------------------------------------- state
@dataclass(frozen=True)
class RoutineState:
    name: str = ""
    spec: RoutineSpec | None = None
    enabled: bool = False
    deleted: bool = False
    next_seq: int = 0
    armed_at: float = 0.0                       # schedules count from here (save / resume time)
    fires: int = 0
    last_session: str | None = None
    last_fired_at: float | None = None
    last_scheduled: Mapping[int, float] = field(default_factory=lambda: MappingProxyType({}))  # trigger index -> last handled scheduled time
    files_seen: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))        # "trigger:relpath" -> signature when it fired
    delivery_keys: frozenset[str] = frozenset()                   # webhook / manual fire keys (dedup of retries)
    fired: tuple[Mapping[str, Any], ...] = ()                     # recent ROUTINE_FIRED payloads (newest last)


RECENT_FIRES = 20


def reduce_routine(state: RoutineState, event: Event) -> RoutineState:
    """Pure fold of one routine event."""
    if event.seq != state.next_seq:
        raise ValueError(f"routine event seq {event.seq} applied to state expecting {state.next_seq}")
    d = event.data
    s = dataclasses.replace(state, next_seq=state.next_seq + 1)
    t = event.type
    if t == ev.ROUTINE_SAVED:
        spec = RoutineSpec.from_dict(d["spec"], validate=False)
        triggers_changed = state.spec is None or state.spec.triggers != spec.triggers
        return dataclasses.replace(
            s, name=spec.name, spec=spec, enabled=True, deleted=False,
            armed_at=float(d["armed_at"]) if triggers_changed or state.deleted else s.armed_at,
            last_scheduled=MappingProxyType({}) if triggers_changed else s.last_scheduled,
        )
    if t in (ev.ROUTINE_FIRED, ev.ROUTINE_SKIPPED):
        last = dict(s.last_scheduled)
        index, scheduled = d.get("trigger_index"), d.get("scheduled_for")
        if index is not None and scheduled is not None:
            last[int(index)] = max(float(scheduled), float(last.get(int(index), 0.0)))
        seen = dict(s.files_seen)
        for key, sig in (d.get("files") or {}).items():
            seen[key] = sig
        keys = s.delivery_keys | {d["fire_key"]} if d.get("kind") in ("webhook", "manual") else s.delivery_keys
        out = dataclasses.replace(s, last_scheduled=MappingProxyType(last), files_seen=MappingProxyType(seen),
                                  delivery_keys=keys)
        if t == ev.ROUTINE_FIRED:
            out = dataclasses.replace(out, fires=s.fires + 1, last_session=d["session_id"],
                                      last_fired_at=float(d["fired_at"]),
                                      fired=(s.fired + (MappingProxyType(dict(d)),))[-RECENT_FIRES:])
        return out
    if t == ev.ROUTINE_PAUSED:
        return dataclasses.replace(s, enabled=False)
    if t == ev.ROUTINE_RESUMED:
        # Re-arm from now: a paused routine does not catch up on the times it skipped while paused.
        return dataclasses.replace(s, enabled=True, armed_at=float(d["armed_at"]), last_scheduled=MappingProxyType({}))
    if t == ev.ROUTINE_DELETED:
        return dataclasses.replace(s, enabled=False, deleted=True)
    raise ValueError(f"unknown routine event type {t!r}")


def replay_routine(events: Iterable[Event]) -> RoutineState:
    state = RoutineState()
    for e in events:
        state = reduce_routine(state, e)
    return state


# --------------------------------------------------------------- due fires
@dataclass(frozen=True)
class Fire:
    fire_key: str
    trigger_index: int | None
    kind: str                       # cron | interval | file | webhook | manual
    scheduled_for: float | None = None
    missed: int = 0                 # earlier scheduled times folded into this one (misfire=run_once)
    files: Mapping[str, str] = field(default_factory=dict)
    payload: Any = None
    skip_reason: str | None = None  # set: record a skip instead of starting a run


def still_due(state: RoutineState, fire: Fire) -> bool:
    """Is `fire` (computed from an older state) still unhandled in `state`?"""
    if fire.kind in ("webhook", "manual"):
        return fire.fire_key not in state.delivery_keys
    if fire.kind == "file":
        return any(k not in state.files_seen for k in fire.files)
    index = fire.trigger_index if fire.trigger_index is not None else -1
    return state.last_scheduled.get(index, 0.0) < (fire.scheduled_for or 0.0)


def file_key(index: int, rel: str) -> str:
    return f"{index}:{rel}"


def scan_files(workspace: Path, spec: RoutineSpec) -> dict[str, str]:
    """Snapshot of the files the routine's file triggers watch: key -> 'mtime_ns:size'."""
    out: dict[str, str] = {}
    for i, trig in enumerate(spec.triggers):
        if trig.kind != "file" or not trig.path:
            continue
        folder = workspace / trig.path
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or not fnmatch.fnmatch(path.name, trig.pattern):
                continue
            if any(part.startswith(".") for part in path.relative_to(folder).parts):
                continue
            st = path.stat()
            out[file_key(i, str(path.relative_to(workspace)))] = f"{st.st_mtime_ns}:{st.st_size}"
    return out


def due(state: RoutineState, now: float, files: Mapping[str, str] | None = None) -> list[Fire]:
    """Fires that are due at `now`. Pure: the file snapshot is passed in.

    A time-based trigger is handled once its scheduled time is recorded (fired
    or skipped), so a fire is never repeated; a file is handled once recorded.
    """
    spec = state.spec
    if spec is None or not state.enabled or state.deleted:
        return []
    out: list[Fire] = []
    tz = spec.tz()
    for i, trig in enumerate(spec.triggers):
        if trig.kind in ("cron", "interval"):
            after = max(state.last_scheduled.get(i, 0.0), state.armed_at)
            times = _scheduled_times(trig, after, now, tz)
            if not times:
                continue
            latest = times[-1]
            key = f"{trig.kind}{i}:{int(latest)}"
            if spec.misfire == "skip" and now - latest > _grace(trig):
                out.append(Fire(key, i, trig.kind, latest, missed=len(times) - 1,
                                skip_reason=f"missed {len(times)} scheduled time(s) while not running"))
            else:
                out.append(Fire(key, i, trig.kind, latest, missed=len(times) - 1))
        elif trig.kind == "file" and files is not None:
            # New paths only: a run that edits or rewrites a file it was given must not trigger itself.
            prefix = f"{i}:"
            new = {k: v for k, v in sorted(files.items()) if k.startswith(prefix) and k not in state.files_seen}
            if new:
                batch = dict(list(new.items())[:MAX_FILES_PER_FIRE])
                out.append(Fire(f"file{i}:{digest(batch, 12)}", i, "file", files=batch))
    return out


def _scheduled_times(trig: Trigger, after: float, now: float, tz: tzinfo) -> list[float]:
    if trig.kind == "cron":
        return Cron.parse(trig.cron or "").times_between(after, now, tz, limit=1000)
    every = float(trig.every_s or 0)
    if every <= 0 or now < after + every:
        return []
    n = int((now - after) // every)
    return [after + k * every for k in range(max(1, n - 999), n + 1)]


def _grace(trig: Trigger) -> float:
    # A fire this late still counts as "on time" (scheduler ticks are not instantaneous).
    return 120.0 if trig.kind == "cron" else min(120.0, (trig.every_s or 60) / 2)


def render_task(template: str, values: Mapping[str, str]) -> str:
    """Replace only the known {placeholders}; any other braces in the task are left alone."""
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), template)


# ---------------------------------------------------------------- scheduler
@dataclass
class FireResult:
    routine: str
    fire_key: str
    session_id: str | None
    started: bool                  # this call started the session (False: already existed / skipped)
    skipped: str | None = None
    deduplicated: bool = False


def stream_id(name: str) -> str:
    return ev.ROUTINE_STREAM_PREFIX + name


class Scheduler:
    """The imperative shell: saves routines, fires due triggers, starts their sessions.

    It never runs sessions itself; callers (the web app, `spool tick`, `spool
    daemon`) run the ids it returns, together with `store.due_sessions(now)`.
    """

    def __init__(self, runtime: Any, *, base_config: AgentConfig | None = None, clock: Any = None) -> None:
        self.rt = runtime
        self.store: EventStore = runtime.store
        self.base_config = base_config or AgentConfig()
        self.clock = clock or runtime.clock
        self._states: dict[str, RoutineState] = {}

    # ------------------------------------------------------------ editing
    def state(self, name: str) -> RoutineState:
        """The folded routine, advanced by whatever was appended since the last read (by anyone)."""
        state = self._states.get(name) or RoutineState()
        for e in self.store.load(stream_id(name), from_seq=state.next_seq):
            state = reduce_routine(state, e)
        self._states[name] = state
        return state

    def _append(self, state: RoutineState, type: str, data: dict[str, Any]) -> Event:
        return self.store.append(stream_id(state.name), type, data, expected_seq=state.next_seq)

    def save(self, spec: RoutineSpec) -> RoutineState:
        spec.validate()
        state = self.state(spec.name)
        state = dataclasses.replace(state, name=spec.name)
        if state.spec == spec and state.enabled and not state.deleted:
            return state  # saving an identical spec is a no-op (apply is idempotent)
        self._append(state, ev.ROUTINE_SAVED, {"spec": spec.to_dict(), "armed_at": self.clock()})
        return self.state(spec.name)

    def pause(self, name: str) -> None:
        state = self._existing(name)
        if state.enabled:
            self._append(state, ev.ROUTINE_PAUSED, {})

    def resume(self, name: str) -> None:
        state = self._existing(name)
        if not state.enabled:
            self._append(state, ev.ROUTINE_RESUMED, {"armed_at": self.clock()})

    def delete(self, name: str) -> None:
        state = self._existing(name)
        self._append(state, ev.ROUTINE_DELETED, {})

    def _existing(self, name: str) -> RoutineState:
        state = self.state(name)
        if state.spec is None or state.deleted:
            raise KeyError(f"no routine {name!r}")
        return state

    def workspace(self, spec: RoutineSpec) -> Path:
        if spec.workspace:
            return Path(os.path.expanduser(spec.workspace))
        return self.rt.data_dir / "routines" / spec.name

    # ------------------------------------------------------------- firing
    def tick(self) -> list[FireResult]:
        """Fire everything due now; also start any fired-but-not-started sessions."""
        results: list[FireResult] = []
        now = self.clock()
        for row in self.store.routines():
            name = row["name"]
            state = self.state(name)
            results.extend(self._repair(state))
            if not state.enabled or state.spec is None:
                continue
            files = None
            if any(t.kind == "file" for t in state.spec.triggers):
                files = scan_files(self.workspace(state.spec), state.spec)
            for fire in due(state, now, files):
                result = self._perform(name, fire)
                if result is not None:
                    results.append(result)
        return results

    def fire(self, name: str, *, payload: Any = None, key: str | None = None, kind: str = "webhook") -> FireResult:
        """Fire a routine now (webhook or manual). With `key`, a repeated delivery is recognised and not re-run."""
        state = self._existing(name)
        if kind == "webhook" and not any(t.kind == "webhook" for t in state.spec.triggers):  # type: ignore[union-attr]
            raise RoutineError(f"routine {name!r} has no webhook trigger")
        fire_key = f"{kind}:{key}" if key else f"{kind}:{digest([self.clock(), payload, state.next_seq], 16)}"
        if fire_key in state.delivery_keys:
            prior = next((f for f in state.fired if f["fire_key"] == fire_key), None)
            return FireResult(name, fire_key, prior["session_id"] if prior else None, False, deduplicated=True)
        result = self._perform(name, Fire(fire_key, None, kind, payload=payload), force=(kind == "manual"))
        assert result is not None
        return result

    def _perform(self, name: str, fire: Fire, *, force: bool = False) -> FireResult | None:
        for _ in range(3):  # re-read and retry if another scheduler appended first
            state = self.state(name)
            spec = state.spec
            if spec is None or (not state.enabled and not force) or not still_due(state, fire):
                return None
            reason = fire.skip_reason
            if reason is None and spec.overlap == "skip" and not force:
                busy = self._busy(state)
                if busy:
                    if fire.kind == "file":
                        return None  # the files stay unseen and fire once the current run is done
                    if fire.kind == "webhook":
                        # Nothing recorded: the sender gets "busy" (HTTP 409) and retries with the same key.
                        return FireResult(name, fire.fire_key, None, False, skipped=f"the previous run {busy} has not finished")
                    reason = f"the previous run {busy} has not finished"
            now = self.clock()
            base = {"fire_key": fire.fire_key, "trigger_index": fire.trigger_index, "kind": fire.kind,
                    "scheduled_for": fire.scheduled_for, "missed": fire.missed}
            try:
                if reason is not None:
                    self._append(state, ev.ROUTINE_SKIPPED, {**base, "reason": reason})
                    return FireResult(name, fire.fire_key, None, False, skipped=reason)
                sid = f"r-{name}-{state.fires + 1:04d}"
                self._append(state, ev.ROUTINE_FIRED, {**base, "fired_at": now, "session_id": sid,
                                                       "payload": fire.payload, "files": dict(fire.files)})
            except ConcurrencyError:
                continue
            started = self._start(spec, sid, fire.fire_key, fire, now)
            return FireResult(name, fire.fire_key, sid, started)
        return None

    def _busy(self, state: RoutineState) -> str | None:
        if state.last_session is None:
            return None
        row = self.store.session(state.last_session)
        if row is None:
            return state.last_session  # fired, not started yet: the repair path will start it
        return state.last_session if row["status"] in UNFINISHED else None

    def _repair(self, state: RoutineState) -> list[FireResult]:
        """Start sessions whose ROUTINE_FIRED was written but whose start was lost to a crash."""
        out = []
        if state.spec is None:
            return out
        for fired in state.fired:
            sid = fired["session_id"]
            if not self.store.exists(sid):
                fire = Fire(fired["fire_key"], fired.get("trigger_index"), fired.get("kind", "manual"),
                            fired.get("scheduled_for"), files=fired.get("files") or {}, payload=fired.get("payload"))
                started = self._start(state.spec, sid, fired["fire_key"], fire, float(fired["fired_at"]))
                out.append(FireResult(state.name, fired["fire_key"], sid, started))
        return out

    def _start(self, spec: RoutineSpec, sid: str, fire_key: str, fire: Fire, fired_at: float) -> bool:
        if self.store.exists(sid):
            return False
        config = self.config_for(spec)
        workspace = self.workspace(spec)
        workspace.mkdir(parents=True, exist_ok=True)
        task = render_task(spec.task, task_values(spec, fire, fired_at))
        try:
            self.rt.start(task, config=config, workspace=workspace, session_id=sid,
                          parent={"kind": "routine", "routine": spec.name, "fire_key": fire_key})
        except ConcurrencyError:
            return False  # another scheduler started it between our check and our append
        return True

    def config_for(self, spec: RoutineSpec) -> AgentConfig:
        """The run's config: the server's defaults, then the routine's overrides.

        Unless the routine says otherwise, actions that need approval go to the
        approvals inbox and expire after approval_ttl_s (whichever process fires
        the routine: the web server, `spool tick` or `spool daemon`).
        """
        merged = self.base_config.to_dict()
        merged.update(dict(spec.config))
        if "approval" not in spec.config:
            merged["approval"] = "human"
        cfg = AgentConfig.from_dict(merged)
        if cfg.approval == "human" and "approval_ttl_s" not in spec.config:
            cfg = cfg.replace(approval_ttl_s=spec.approval_ttl_s)
        return cfg

    # ------------------------------------------------------------ reading
    def next_times(self, state: RoutineState, now: float) -> list[float]:
        """Upcoming scheduled times (one per time-based trigger), for display."""
        spec = state.spec
        if spec is None or not state.enabled:
            return []
        out = []
        tz = spec.tz()
        for i, trig in enumerate(spec.triggers):
            after = max(state.last_scheduled.get(i, 0.0), state.armed_at, now)
            if trig.kind == "cron":
                out.append(Cron.parse(trig.cron or "").next_after(after, tz))
            elif trig.kind == "interval":
                base = max(state.last_scheduled.get(i, 0.0), state.armed_at)
                every = float(trig.every_s or 0)
                k = max(int((now - base) // every) + 1, 1)
                out.append(base + k * every)
        return sorted(out)


def task_values(spec: RoutineSpec, fire: Fire, fired_at: float) -> dict[str, str]:
    import json

    local = datetime.fromtimestamp(fire.scheduled_for or fired_at, spec.tz())
    files = sorted(key.split(":", 1)[1] for key in fire.files)
    return {
        "date": local.strftime("%Y-%m-%d"),
        "time": local.strftime("%H:%M"),
        "now": iso(fired_at),
        "files": "\n".join(files) if files else "(none)",
        "payload": json.dumps(fire.payload, ensure_ascii=False) if fire.payload is not None else "(none)",
    }


def specs_from_toml(data: Mapping[str, Any]) -> list[RoutineSpec]:
    """`[[routine]]` tables -> specs. Each routine may also use the shorthands cron=, every=, watch=, webhook=."""
    out = []
    for raw in data.get("routine") or []:
        raw = dict(raw)
        triggers = [dict(t) for t in raw.pop("trigger", [])]
        if "cron" in raw:
            triggers.append({"kind": "cron", "cron": raw.pop("cron")})
        if "every" in raw:
            triggers.append({"kind": "interval", "every_s": float(raw.pop("every"))})
        if "watch" in raw:
            triggers.append({"kind": "file", "path": raw.pop("watch"), "pattern": raw.pop("pattern", "*")})
        if raw.pop("webhook", False):
            triggers.append({"kind": "webhook"})
        raw["triggers"] = triggers
        out.append(RoutineSpec.from_dict(raw))
    return out


def summarize_routine(state: RoutineState, store: EventStore, now: float, scheduler: Scheduler) -> dict[str, Any]:
    spec = state.spec
    assert spec is not None
    last = store.session(state.last_session) if state.last_session else None
    runs = []
    for fired in reversed(state.fired):
        row = store.session(fired["session_id"])
        runs.append({"session_id": fired["session_id"], "fired_at": fired["fired_at"], "kind": fired.get("kind"),
                     "scheduled_for": fired.get("scheduled_for"), "status": row["status"] if row else "not started"})
    return {
        "name": spec.name,
        "spec": spec.to_dict(),
        "triggers": [t.describe() for t in spec.triggers],
        "enabled": state.enabled,
        "fires": state.fires,
        "next_at": (scheduler.next_times(state, now) or [None])[0],
        "last_session": state.last_session,
        "last_status": last["status"] if last else None,
        "last_fired_at": state.last_fired_at,
        "runs": runs,
        "workspace": str(scheduler.workspace(spec)),
    }


def describe_triggers(triggers: Sequence[Trigger]) -> str:
    return ", ".join(t.describe() for t in triggers) or "manual only"
