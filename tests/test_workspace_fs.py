"""Commit 2: sandbox + filesystem tools."""
import os

import pytest

from sonic.errors import SandboxError, ToolError
from sonic.tools import fs
from sonic.workspace import Workspace


# 1
@pytest.mark.parametrize("bad", ["../outside.txt", "a/../../outside.txt", "/etc/passwd"])
def test_workspace_rejects_paths_outside_root(tmp_path, bad):
    ws = Workspace(tmp_path)
    with pytest.raises(SandboxError):
        ws.resolve(bad)
    assert ws.resolve("sub/ok.txt") == tmp_path.resolve() / "sub" / "ok.txt"


# 2
def test_workspace_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    os.symlink(outside, root / "link.txt")
    ws = Workspace(root)
    with pytest.raises(SandboxError):
        ws.resolve("link.txt")
    with pytest.raises(SandboxError):
        fs.read_file(ws, "link.txt")


# 3
def test_write_then_read_roundtrip_creates_parent_dirs(tmp_path):
    ws = Workspace(tmp_path)
    msg = fs.write_file(ws, "a/b/hello.py", "print('hi')\n")
    assert "a/b/hello.py" in msg
    assert (tmp_path / "a/b/hello.py").read_text() == "print('hi')\n"
    assert fs.read_file(ws, "a/b/hello.py") == "print('hi')\n"
    assert fs.list_dir(ws, ".").splitlines() == ["a/"]
    with pytest.raises(ToolError):
        fs.read_file(ws, "missing.txt")


# 4
def test_edit_file_requires_exactly_one_match(tmp_path):
    ws = Workspace(tmp_path)
    fs.write_file(ws, "f.txt", "one two two")
    with pytest.raises(ToolError, match="not found"):
        fs.edit_file(ws, "f.txt", "three", "3")
    with pytest.raises(ToolError, match="2"):  # message reports the match count
        fs.edit_file(ws, "f.txt", "two", "2")
    fs.edit_file(ws, "f.txt", "one", "1")
    assert (tmp_path / "f.txt").read_text() == "1 two two"
