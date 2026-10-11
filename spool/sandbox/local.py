"""Subprocess sandbox, optionally confined by a macOS Seatbelt profile."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Sequence

from . import ExecResult

DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
MAX_OUTPUT_BYTES = 1_000_000
# Credentials a shell inside the sandbox should never be able to read.
HOME_SECRETS = (".ssh", ".aws", ".gnupg", ".config/gcloud", ".kube", ".docker", ".netrc",
                ".config/anthropic", ".claude", "Library/Keychains")


def seatbelt_profile(workspace: Path, protected: Sequence[Path]) -> str:
    ws = str(workspace)
    home = os.path.realpath(os.path.expanduser("~"))
    lines = [
        "(version 1)",
        "(allow default)",
        # No network at all, loopback included: the model server and other local
        # services are as off-limits as the internet.
        "(deny network*)",
        "(allow network* (remote unix-socket))",
        # Writes only inside the workspace (TMPDIR points into it) and a few devices.
        # Shared temp dirs stay read-only: other sessions' workspaces may live there.
        "(deny file-write*)",
        "(allow file-write*",
        f'  (subpath "{ws}")',
        '  (literal "/dev/null") (literal "/dev/zero") (literal "/dev/stdout") (literal "/dev/stderr")',
        '  (literal "/dev/dtracehelper") (regex #"^/dev/tty")',
        # xcrun (behind /usr/bin/git, clang, ...) keeps a lookup cache in the user temp dir.
        '  (regex #"/xcrun_db"))',
    ]
    for path in protected:
        lines.append(f'(deny file-read* file-write* (subpath "{path}"))')
    for rel in HOME_SECRETS:
        lines.append(f'(deny file-read* (subpath "{home}/{rel}"))')
    return "\n".join(lines) + "\n"


class LocalSandbox:
    def __init__(self, workspace: Path, *, seatbelt: bool = False, protected: Sequence[str] = (),
                 max_output_bytes: int = MAX_OUTPUT_BYTES) -> None:
        self.workspace = Path(os.path.realpath(workspace))
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.seatbelt = seatbelt
        self.isolation = "seatbelt" if seatbelt else "process"
        self.max_output_bytes = max_output_bytes
        self.protected = [self.workspace / p for p in protected]
        self._tmp = self.workspace / ".spool" / "tmp"
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._profile = seatbelt_profile(self.workspace, self.protected) if seatbelt else None

    def _env(self) -> dict[str, str]:
        # Built from scratch: nothing from the harness's environment (API keys,
        # tokens) leaks into the agent's shell.
        return {
            "PATH": DEFAULT_PATH,
            "HOME": str(self.workspace),
            "TMPDIR": str(self._tmp),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TERM": "dumb",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
            "PAGER": "cat",
            "GIT_PAGER": "cat",
        }

    def argv(self, command: str) -> list[str]:
        argv = ["/bin/bash", "--noprofile", "--norc", "-c", command]
        if self._profile is not None:
            argv = ["/usr/bin/sandbox-exec", "-p", self._profile, *argv]
        return argv

    async def exec(self, command: str, timeout: float) -> ExecResult:
        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *self.argv(command),
            cwd=self.workspace,
            env=self._env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group, so a timeout can kill children too
        )
        out_task = asyncio.create_task(_read_capped(proc.stdout, self.max_output_bytes))
        err_task = asyncio.create_task(_read_capped(proc.stderr, self.max_output_bytes))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            _kill_group(proc.pid)
            await proc.wait()
        except asyncio.CancelledError:
            _kill_group(proc.pid)
            raise
        (stdout, out_trunc), (stderr, err_trunc) = await asyncio.gather(out_task, err_task)
        return ExecResult(
            exit_code=None if timed_out else proc.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_ms=(time.perf_counter() - started) * 1000,
            truncated=out_trunc or err_trunc,
        )

    async def close(self) -> None:
        return None


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _read_capped(stream: asyncio.StreamReader | None, cap: int) -> tuple[str, bool]:
    if stream is None:
        return "", False
    chunks: list[bytes] = []
    size = 0
    truncated = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if size < cap:
            chunks.append(chunk[: cap - size])
        if size + len(chunk) > cap:
            truncated = True
        size += len(chunk)
    return b"".join(chunks).decode("utf-8", "replace"), truncated
