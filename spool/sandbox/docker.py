"""Docker sandbox: one hardened, long-lived container per session."""

from __future__ import annotations

import asyncio
import os
import shlex
import time
import uuid
from pathlib import Path
from typing import Sequence

from . import ExecResult
from .local import _read_capped

DEFAULT_IMAGE = os.environ.get("SPOOL_DOCKER_IMAGE", "python:3.12-slim")


class DockerSandbox:
    """Commands run through `docker exec` inside a container started with:

    --network none            no network at all
    --read-only               immutable root filesystem; /tmp is a small tmpfs
    --cap-drop ALL            no Linux capabilities
    --security-opt no-new-privileges
    --pids-limit / --memory / --cpus   resource ceilings
    --user <uid>:<gid>        not root, and files written in the workspace keep the host owner
    Protected workspace dirs are shadowed by empty read-only tmpfs mounts, so
    bash cannot see them even though the workspace itself is bind-mounted.
    """

    def __init__(self, workspace: Path, *, protected: Sequence[str] = (), image: str = DEFAULT_IMAGE,
                 memory: str = "1g", cpus: str = "1", pids: int = 256, max_output_bytes: int = 1_000_000) -> None:
        self.workspace = Path(os.path.realpath(workspace))
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.isolation = "docker"
        self.image = image
        self.protected = list(protected)
        self.memory, self.cpus, self.pids = memory, cpus, pids
        self.max_output_bytes = max_output_bytes
        self.name = f"spool-{uuid.uuid4().hex[:10]}"
        self._started = False
        self._start_lock = asyncio.Lock()

    def run_argv(self) -> list[str]:
        argv = [
            "docker", "run", "-d", "--rm", "--name", self.name,
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(self.pids), "--memory", self.memory, "--cpus", self.cpus,
            "--tmpfs", "/tmp:rw,size=256m",
            "-v", f"{self.workspace}:/workspace", "-w", "/workspace",
            "-e", "HOME=/workspace", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "--user", f"{os.getuid()}:{os.getgid()}",
        ]
        for rel in self.protected:
            argv += ["--tmpfs", f"/workspace/{rel}:ro,size=1k"]
        argv += [self.image, "sleep", "infinity"]
        return argv

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            proc = await asyncio.create_subprocess_exec(
                *self.run_argv(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, err = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"docker run failed: {err.decode(errors='replace').strip()}")
            self._started = True

    async def exec(self, command: str, timeout: float) -> ExecResult:
        await self._ensure_started()
        started = time.perf_counter()
        # `timeout` inside the container kills the command itself; killing the
        # docker CLI on the host would leave the process running in the container.
        inner = f"timeout -s KILL {max(int(timeout), 1)} bash -c {shlex.quote(command)}"
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-w", "/workspace", self.name, "bash", "-c", inner,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out_task = asyncio.create_task(_read_capped(proc.stdout, self.max_output_bytes))
        err_task = asyncio.create_task(_read_capped(proc.stderr, self.max_output_bytes))
        try:
            await asyncio.wait_for(proc.wait(), timeout + 10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        (stdout, t1), (stderr, t2) = await asyncio.gather(out_task, err_task)
        elapsed = time.perf_counter() - started
        # `timeout -s KILL` exits 137; only count it as a timeout if the clock agrees,
        # since a command can exit 137 on its own.
        timed_out = proc.returncode in (124, 137, -9) and elapsed >= timeout * 0.95
        return ExecResult(
            exit_code=None if timed_out else proc.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_ms=(time.perf_counter() - started) * 1000,
            truncated=t1 or t2,
        )

    async def close(self) -> None:
        if not self._started:
            return
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", self.name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()
        self._started = False
