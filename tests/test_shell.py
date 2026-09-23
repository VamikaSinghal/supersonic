"""Commit 3: shell tool."""
import pytest

from sonic.errors import ToolError
from sonic.tools.shell import run_shell
from sonic.workspace import Workspace


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
