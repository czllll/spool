# ADR 0005: Three sandbox backends behind one interface

Status: accepted

## Context

The agent's shell must not be able to write outside its workspace, reach the network, or read the
user's credentials. The project is developed on macOS (no KVM, so no Firecracker locally) and
should also run in containers.

## Decision

`Sandbox.exec(command, timeout)` with three backends (`spool/sandbox/`):

| backend  | isolation | used for |
|---|---|---|
| process  | none beyond a scrubbed environment, a process group killed on timeout, capped output | trusted tasks, CI without other options |
| seatbelt | macOS `sandbox-exec` profile: writes only in the workspace, no network (loopback included), no reads of protected workspace dirs or `~/.ssh`, `~/.aws`, ... | default on macOS |
| docker   | long-lived container per session: `--network none`, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, pids/memory/cpu limits, non-root user, protected dirs shadowed by empty tmpfs | Linux and stronger isolation |

File tools run in the harness with path checks (`resolve_path` refuses `..`, absolute paths and
symlink escapes); the sandbox's job is to stop `bash` from going around them.

## Consequences

- `sandbox-exec` is deprecated by Apple but still ships and is what several agent CLIs use on
  macOS; the interface lets it be replaced.
- Shared temp dirs are read-only inside Seatbelt (another session's workspace may live there);
  `TMPDIR` points into the workspace instead.
- Docker timeouts are enforced with `timeout -s KILL` inside the container, because killing the
  `docker exec` client would leave the process running.

## Revisit when

Running untrusted code for many tenants: use microVMs (Firecracker via a cloud sandbox provider)
with snapshot/restore for fast start, and keep the same interface.
