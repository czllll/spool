import os
import shutil
import subprocess
import time

import pytest

from spool.sandbox import docker_available, seatbelt_available
from spool.sandbox.local import LocalSandbox

from .conftest import run

needs_seatbelt = pytest.mark.skipif(not seatbelt_available(), reason="macOS sandbox-exec not available")


def test_process_sandbox_basics(workspace):
    sb = LocalSandbox(workspace)
    r = run(sb.exec("echo out; echo err >&2; exit 3", timeout=10))
    assert r.stdout == "out\n" and r.stderr == "err\n" and r.exit_code == 3 and not r.timed_out


def test_environment_is_scrubbed(workspace, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    r = run(LocalSandbox(workspace).exec("env", timeout=10))
    assert "sk-should-not-leak" not in r.stdout and f"HOME={os.path.realpath(workspace)}" in r.stdout


def test_timeout_kills_the_whole_process_group(workspace):
    sb = LocalSandbox(workspace)
    started = time.monotonic()
    r = run(sb.exec("(sleep 30; echo late > late.txt) & sleep 30", timeout=1))
    assert r.timed_out and time.monotonic() - started < 10
    time.sleep(0.5)
    assert not (workspace / "late.txt").exists()


def test_output_is_capped(workspace):
    r = run(LocalSandbox(workspace, max_output_bytes=1000).exec("yes | head -c 100000", timeout=10))
    assert len(r.stdout) == 1000 and r.truncated


@needs_seatbelt
def test_seatbelt_confines_writes_reads_and_network(workspace, tmp_path):
    (workspace / "secrets").mkdir()
    (workspace / "secrets" / "key.txt").write_text("TOPSECRET")
    outside = tmp_path / "outside.txt"
    sb = LocalSandbox(workspace, seatbelt=True, protected=["secrets"])
    assert run(sb.exec("echo ok > inside.txt && cat inside.txt", timeout=10)).stdout == "ok\n"
    r = run(sb.exec(f"echo x > {outside}", timeout=10))
    assert r.exit_code != 0 and not outside.exists()
    r = run(sb.exec("cat secrets/key.txt", timeout=10))
    assert "TOPSECRET" not in r.stdout and r.exit_code != 0
    r = run(sb.exec("python3 -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:9', timeout=2)\"", timeout=15))
    assert r.exit_code != 0 and ("Operation not permitted" in r.stderr or "PermissionError" in r.stderr)
    assert run(sb.exec("python3 -c 'print(6*7)'", timeout=15)).stdout == "42\n"


def _docker_ready() -> bool:
    if not docker_available():
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(not _docker_ready(), reason="docker daemon not available")
def test_docker_sandbox(workspace):
    from spool.sandbox.docker import DockerSandbox

    (workspace / "secrets").mkdir()
    (workspace / "secrets" / "key.txt").write_text("TOPSECRET")
    sb = DockerSandbox(workspace, protected=["secrets"])
    try:
        r = run(sb.exec("echo hi > from_container.txt && cat from_container.txt", timeout=60))
        if r.exit_code == 125 or "Unable to find image" in r.stderr:
            pytest.skip("docker image not available offline")
        assert r.stdout == "hi\n"
        assert (workspace / "from_container.txt").read_text() == "hi\n"
        assert "TOPSECRET" not in run(sb.exec("cat secrets/key.txt; ls -la secrets", timeout=30)).stdout
        net = run(sb.exec("python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), 2)\"", timeout=30))
        assert net.exit_code != 0
        assert run(sb.exec("touch /etc/x", timeout=30)).exit_code != 0  # read-only root
        assert run(sb.exec("sleep 20", timeout=2)).timed_out
    finally:
        run(sb.close())
