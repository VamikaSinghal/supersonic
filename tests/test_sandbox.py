"""OS-level no-deletion sandbox.

Every destructive payload here targets only canary files inside pytest's tmp_path
(and HOME is faked by conftest), so a sandbox failure can at worst delete a canary.
"""
import base64
import subprocess
import sys

import pytest

from sonic import sandbox
from sonic.errors import ToolError
from sonic.tools.shell import run_shell
from sonic.workspace import Workspace

needs_sandbox = pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    inside = root / "keep.txt"
    inside.write_text("keep")
    canary = tmp_path / "outside" / "canary.txt"
    canary.write_text("canary")
    return Workspace(root), inside, canary


def raw(ws, cmd):
    """Run through the sandbox only, bypassing the text denylist, to test the kernel layer."""
    return subprocess.run(sandbox.wrap(cmd, ws), cwd=ws.root, env=sandbox.env(ws),
                          capture_output=True, text=True, timeout=10)


# 18
@needs_sandbox
@pytest.mark.parametrize("template", [
    "rm -f {p}",
    "rm -rf {d}",
    "python3 -c \"import os; os.remove('{p}')\"",
    "python3 -c \"import shutil; shutil.rmtree('{d}')\"",
    "echo {b64} | base64 -d | sh",
])
@pytest.mark.parametrize("where", ["inside", "outside"])
def test_sandbox_blocks_all_deletion_even_obfuscated(box, template, where):
    ws, inside, canary = box
    target = inside if where == "inside" else canary
    b64 = base64.b64encode(f"rm -f {target}".encode()).decode()
    raw(ws, template.format(p=target, d=target.parent, b64=b64))
    assert target.read_text() in ("keep", "canary")


# 19
@needs_sandbox
def test_sandbox_allows_workspace_writes_and_scratch_but_not_outside_writes(box):
    ws, inside, canary = box
    assert raw(ws, "echo new > out.txt").returncode == 0
    assert (ws.root / "out.txt").read_text() == "new\n"
    raw(ws, f"echo pwned > {canary}")
    assert canary.read_text() == "canary"
    r = raw(ws, 'f="$TMPDIR/t.txt"; echo x > "$f" && rm "$f" && echo ok')
    assert r.returncode == 0 and "ok" in r.stdout


# 20
@needs_sandbox
def test_run_shell_is_sandboxed(box):
    ws, inside, _ = box
    assert run_shell(ws, "echo hi").stdout.strip() == "hi"
    r = run_shell(ws, "python3 -c \"import os; os.remove('keep.txt')\"")
    assert r.exit_code != 0
    assert inside.read_text() == "keep"


# 21
def test_run_shell_fails_closed_without_sandbox(box, monkeypatch):
    ws, _, _ = box
    monkeypatch.setattr(sandbox, "available", lambda: False)
    with pytest.raises(ToolError, match="--no-sandbox"):
        run_shell(ws, "echo hi")
    assert run_shell(Workspace(ws.root, sandboxed=False), "echo hi").stdout.strip() == "hi"
