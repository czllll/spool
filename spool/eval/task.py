"""Eval task format.

    evals/tasks/<suite>/<name>/
        task.toml    prompt and options (see Task)
        setup/       copied into a fresh workspace before the trial
        web/         pages served by the offline web_fetch fixture
        services/    seed data for the demo services (mail.json, calendar.json, shop.json)
        verify.py    run after the trial: `python3 verify.py <workspace> <ledger_dir>`,
                     exit 0 = pass, first stdout line = reason
    evals/tasks/<suite>/connectors.toml   connectors the suite's tasks may enable

Tasks that use connectors also say how the simulated user answers approvals:

    connectors = ["mail", "calendar"]
    [approver]
    default = "approve"            # approve | deny | ignore (let the request expire)
    deny = ["shop__place_order"]   # per-tool exceptions

The verifier checks the outcome (files, sent messages), never the path the
agent took, and it lives outside the workspace so the agent cannot read it.
"""

from __future__ import annotations

import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Task:
    id: str
    dir: Path
    prompt: str
    max_steps: int | None = None
    tools: tuple[str, ...] | None = None
    tags: tuple[str, ...] = ()
    timeout_s: float = 900.0
    web: dict[str, str] = field(default_factory=dict)  # url -> page text
    overrides: dict[str, Any] = field(default_factory=dict)
    connectors: tuple[str, ...] = ()
    approver: dict[str, Any] = field(default_factory=dict)
    skills: tuple[str, ...] = ()

    @property
    def skills_dirs(self) -> list[Path]:
        return [d for d in (self.dir / "skills", self.dir.parent / "skills") if d.is_dir()]

    def approval(self, tool: str) -> str:
        """How the simulated user answers an approval request for `tool`."""
        for mode in ("deny", "ignore", "approve"):
            if tool in (self.approver.get(mode) or ()):
                return mode
        return self.approver.get("default", "approve")

    @property
    def connectors_file(self) -> Path | None:
        for candidate in (self.dir / "connectors.toml", self.dir.parent / "connectors.toml"):
            if candidate.exists():
                return candidate
        return None

    @property
    def suite(self) -> str:
        return self.id.split("/")[0]

    def prepare_workspace(self, workspace: Path) -> None:
        workspace.mkdir(parents=True, exist_ok=True)
        setup = self.dir / "setup"
        if setup.exists():
            shutil.copytree(setup, workspace, dirs_exist_ok=True)

    def fixtures(self) -> dict[str, Any]:
        return {"web": dict(self.web)} if self.web else {}


def load_task(task_dir: str | Path, suite_root: Path | None = None) -> Task:
    task_dir = Path(task_dir).resolve()  # verify.py runs with the workspace as cwd
    with open(task_dir / "task.toml", "rb") as fh:
        raw = tomllib.load(fh)
    web: dict[str, str] = {}
    for url, rel in (raw.get("web") or {}).items():
        web[url] = (task_dir / rel).read_text(encoding="utf-8")
    default_id = f"{task_dir.parent.name}/{task_dir.name}"
    return Task(
        id=raw.get("id", default_id),
        dir=task_dir,
        prompt=raw["prompt"].strip(),
        max_steps=raw.get("max_steps"),
        tools=tuple(raw["tools"]) if "tools" in raw else None,
        tags=tuple(raw.get("tags", ())),
        timeout_s=float(raw.get("timeout_s", 900)),
        web=web,
        overrides=dict(raw.get("config") or {}),
        connectors=tuple(raw.get("connectors") or ()),
        approver=dict(raw.get("approver") or {}),
        skills=tuple(raw.get("skills") or ()),
    )


def load_suite(path: str | Path, pattern: str | None = None) -> list[Task]:
    """A suite dir holds task dirs; a task dir is loaded alone. `pattern` filters by substring."""
    path = Path(path)
    if (path / "task.toml").exists():
        tasks = [load_task(path)]
    else:
        tasks = []
        for child in sorted(path.iterdir()):
            if (child / "task.toml").exists():
                tasks.append(load_task(child))
            elif child.is_dir() and child.name != "skills" and any(
                    (g / "task.toml").exists() for g in child.iterdir() if g.is_dir()):
                tasks.extend(load_suite(child))
    if pattern:
        tasks = [t for t in tasks if pattern in t.id]
    return tasks
