"""Where the agent's shell commands run.

Three backends share one interface:
  process   plain subprocess in the workspace: timeouts, process-group kill, scrubbed env.
            No filesystem or network isolation; use only for trusted tasks.
  seatbelt  macOS sandbox-exec profile on top of `process`: writes only inside the
            workspace and temp dirs, no network, no reads of protected paths
            (the workspace's sensitive dirs, ~/.ssh, cloud credentials).
  docker    a long-lived hardened container per session (no network, read-only root,
            all capabilities dropped, pids/memory/cpu limits); commands run via exec.

File tools (read/write/edit) run in the harness itself with path checks, so the
policy engine sees every access to a sensitive path. The sandbox's job is to make
`bash` unable to go around them.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


@dataclass
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: float
    truncated: bool = False


class Sandbox(Protocol):
    workspace: Path
    isolation: str

    async def exec(self, command: str, timeout: float) -> ExecResult: ...

    async def close(self) -> None: ...


def seatbelt_available() -> bool:
    return sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists()


def docker_available() -> bool:
    return shutil.which("docker") is not None


def make_sandbox(kind: str, workspace: Path, *, protected: Sequence[str] = ()) -> Sandbox:
    from .local import LocalSandbox

    if kind == "auto":
        kind = "seatbelt" if seatbelt_available() else "process"
    if kind == "process":
        return LocalSandbox(workspace, seatbelt=False, protected=protected)
    if kind == "seatbelt":
        if not seatbelt_available():
            raise RuntimeError("seatbelt sandbox requires macOS with /usr/bin/sandbox-exec")
        return LocalSandbox(workspace, seatbelt=True, protected=protected)
    if kind == "docker":
        from .docker import DockerSandbox

        return DockerSandbox(workspace, protected=protected)
    raise ValueError(f"unknown sandbox kind {kind!r}")
