"""Commit 3: shell tool."""
import subprocess

import pytest

from sonic.errors import ToolError
from sonic.tools.shell import run_shell
from sonic.workspace import Workspace


@pytest.fixture(autouse=True)
def never_execute_blocked(request, monkeypatch):
    """Denylist tests must fail, never run, if a dangerous command slips through."""
    if "blocks" not in request.node.name:
        return
    def boom(*a, **k):
        raise AssertionError(f"dangerous command reached subprocess: {a!r}")
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)


# 5
def test_run_shell_captures_output_exit_code_and_uses_root_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    ws = Workspace(tmp_path)
    r = run_shell(ws, "ls; echo oops >&2; exit 3")
    assert r.exit_code == 3
    assert "marker.txt" in r.stdout
    assert "oops" in r.stderr
    assert not r.timed_out
    assert "3" in str(r) and "marker.txt" in str(r)


# 6
def test_run_shell_times_out(tmp_path):
    r = run_shell(Workspace(tmp_path), "sleep 5", timeout=0.5)
    assert r.timed_out
    assert r.exit_code is None
    assert "timed out" in str(r).lower()


# 7
@pytest.mark.parametrize("cmd", ["rm -rf /", "sudo ls", "rm -rf ~", ":(){ :|:& };:"])
def test_run_shell_blocks_dangerous_commands(tmp_path, cmd):
    with pytest.raises(ToolError, match="(?i)blocked"):
        run_shell(Workspace(tmp_path), cmd)


# 13
@pytest.mark.parametrize("cmd", [
    'bash -c "sudo ls"',
    "sh -c 'rm -rf ~'",
    "eval 'rm -rf /'",
    "echo cm0gLXJmIC8= | base64 -d | sh",
    "python3 -c \"import shutil; shutil.rmtree('/')\"",
])
def test_run_shell_blocks_wrapped_dangerous_commands(tmp_path, cmd):
    with pytest.raises(ToolError, match="(?i)blocked"):
        run_shell(Workspace(tmp_path), cmd)


# 14
def test_run_shell_allows_benign_wrappers_and_reports_timeout_duration(tmp_path):
    ws = Workspace(tmp_path)
    assert run_shell(ws, 'bash -c "echo hi"').stdout.strip() == "hi"
    assert run_shell(ws, 'python3 -c "print(1)"').stdout.strip() == "1"
    assert "timed out after 0.5s" in str(run_shell(ws, "sleep 5", timeout=0.5))


# 22
@pytest.mark.parametrize("cmd", [
    "rm notes.txt", "rmdir build", "unlink a.txt", "find . -name '*.log' -delete",
    "git clean -fdx", "shred -u secret.txt", "ls && rm -r src",
])
def test_run_shell_blocks_plain_deletion_commands(tmp_path, cmd):
    with pytest.raises(ToolError, match="(?i)blocked"):
        run_shell(Workspace(tmp_path), cmd)
