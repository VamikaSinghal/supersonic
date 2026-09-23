"""Edge cases for filesystem tools, the workspace, and undo.

Every failure a user (or planner) can trigger must surface as ToolError with a
clear message; no other exception type may escape an fs tool.
"""
import os
import sys

import pytest

from sonic.errors import SandboxError, ToolError
from sonic.planner import Action
from sonic.tools import fs
from sonic.undo import TRASH_DIR, UndoStack
from sonic.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path)


def trashed(root):
    trash = root / TRASH_DIR
    return sorted(p.read_bytes() for p in trash.rglob("*") if p.is_file()) if trash.exists() else []


def apply(stack, ws, action):
    change = stack.snapshot(action)
    getattr(fs, action.tool)(ws, **action.args)
    stack.push(change)


# --- reading -----------------------------------------------------------------

def test_empty_file_read_and_edit(ws, tmp_path):
    (tmp_path / "empty.txt").write_bytes(b"")
    assert fs.read_file(ws, "empty.txt") == ""
    with pytest.raises(ToolError, match="not found"):
        fs.edit_file(ws, "empty.txt", "x", "y")
    assert fs.write_file(ws, "e2.txt", "") == "wrote 0 bytes to e2.txt"
    assert (tmp_path / "e2.txt").read_bytes() == b""


def test_read_exactly_at_limit_is_not_truncated(ws, tmp_path):
    (tmp_path / "at.txt").write_bytes(b"a" * fs.MAX_READ_BYTES)
    out = fs.read_file(ws, "at.txt")
    assert out == "a" * fs.MAX_READ_BYTES
    assert "truncated" not in out


def test_read_one_byte_over_limit_is_truncated(ws, tmp_path):
    (tmp_path / "over.txt").write_bytes(b"a" * (fs.MAX_READ_BYTES + 1))
    out = fs.read_file(ws, "over.txt")
    assert out.startswith("a" * fs.MAX_READ_BYTES)
    assert f"[truncated: showing {fs.MAX_READ_BYTES} of {fs.MAX_READ_BYTES + 1} bytes]" in out


def test_truncation_never_splits_a_multibyte_character(ws, tmp_path):
    (tmp_path / "cjk.txt").write_text("a" + "漢" * fs.MAX_READ_BYTES, encoding="utf-8")
    out = fs.read_file(ws, "cjk.txt")
    assert "�" not in out and "truncated" in out


@pytest.mark.parametrize("name", ["héllo wörld.txt", "emoji 🚀.md", "漢字/ファイル.txt", "dir with spaces/f g.txt"])
def test_unicode_and_space_names_roundtrip(ws, tmp_path, name):
    content = "κόσμε 🚀 漢字\n"
    msg = fs.write_file(ws, name, content)
    assert name in msg
    assert fs.read_file(ws, name) == content
    fs.edit_file(ws, name, "🚀", "✨")
    assert (tmp_path / name).read_text(encoding="utf-8") == "κόσμε ✨ 漢字\n"
    parent = os.path.dirname(name) or "."
    assert os.path.basename(name) in fs.list_dir(ws, parent).splitlines()


# --- "" and "." and directories ---------------------------------------------

@pytest.mark.parametrize("path", ["", ".", "./", "sub", "sub/"])
def test_directory_paths_are_tool_errors(ws, tmp_path, path):
    (tmp_path / "sub").mkdir()
    with pytest.raises(ToolError, match="directory"):
        fs.read_file(ws, path)
    with pytest.raises(ToolError, match="directory"):
        fs.write_file(ws, path, "x")
    with pytest.raises(ToolError, match="directory"):
        fs.edit_file(ws, path, "a", "b")


def test_write_where_parent_is_a_file(ws, tmp_path):
    (tmp_path / "f.txt").write_text("keep")
    with pytest.raises(ToolError, match="not a directory"):
        fs.write_file(ws, "f.txt/child.txt", "x")
    with pytest.raises(ToolError, match="not a directory"):
        fs.write_file(ws, "f.txt/deeper/child.txt", "x")
    assert (tmp_path / "f.txt").read_text() == "keep"
    with pytest.raises(ToolError):
        fs.read_file(ws, "f.txt/child.txt")
    with pytest.raises(ToolError):
        fs.list_dir(ws, "f.txt")


# --- reserved .sonic ---------------------------------------------------------

@pytest.mark.parametrize("path", [
    ".sonic/context/graph.json",
    ".sonic/trash/1/a.txt",
    ".sonic/sessions/x.jsonl",
    ".sonic/new.txt",
    "a/../.sonic/x",
    "./.sonic/x",
])
def test_writes_into_sonic_are_refused(ws, tmp_path, path):
    (tmp_path / "a").mkdir()
    (tmp_path / ".sonic/context").mkdir(parents=True)
    (tmp_path / ".sonic/context/graph.json").write_text("{}")
    with pytest.raises(ToolError, match="reserved"):
        fs.write_file(ws, path, "evil")
    assert (tmp_path / ".sonic/context/graph.json").read_text() == "{}"
    assert not (tmp_path / ".sonic/new.txt").exists()
    assert not (tmp_path / ".sonic/x").exists()


def test_edit_into_sonic_refused_but_read_allowed(ws, tmp_path):
    (tmp_path / ".sonic/context").mkdir(parents=True)
    (tmp_path / ".sonic/context/graph.json").write_text('{"a": 1}')
    assert fs.read_file(ws, ".sonic/context/graph.json") == '{"a": 1}'
    assert "context/" in fs.list_dir(ws, ".sonic")
    with pytest.raises(ToolError, match="reserved"):
        fs.edit_file(ws, ".sonic/context/graph.json", "1", "2")
    assert (tmp_path / ".sonic/context/graph.json").read_text() == '{"a": 1}'


@pytest.mark.parametrize("path", [".SONIC/x", ".Sonic/context/graph.json", "a/../.sOnIc/x"])
def test_case_variants_of_sonic_refused(ws, tmp_path, path):
    # Refused on every filesystem (on case-insensitive ones, e.g. macOS APFS,
    # these name the real .sonic dir; on case-sensitive ones refusing is harmless).
    (tmp_path / "a").mkdir()
    (tmp_path / ".sonic/context").mkdir(parents=True)
    (tmp_path / ".sonic/context/graph.json").write_text("{}")
    with pytest.raises(ToolError, match="reserved"):
        fs.write_file(ws, path, "evil")
    assert (tmp_path / ".sonic/context/graph.json").read_text() == "{}"


def test_symlink_into_sonic_refused(ws, tmp_path):
    (tmp_path / ".sonic").mkdir()
    os.symlink(tmp_path / ".sonic", tmp_path / "harmless")
    with pytest.raises(ToolError, match="reserved"):
        fs.write_file(ws, "harmless/x.txt", "evil")
    assert not (tmp_path / ".sonic/x.txt").exists()


def test_sonic_prefixed_names_are_not_reserved(ws, tmp_path):
    fs.write_file(ws, ".sonicrc", "ok")
    fs.write_file(ws, "docs/.sonic/x", "ok")  # only the top-level .sonic is reserved
    assert (tmp_path / ".sonicrc").read_text() == "ok"


# --- symlinks -----------------------------------------------------------------

def test_symlink_to_in_workspace_file_read_and_edit(ws, tmp_path):
    (tmp_path / "real.txt").write_text("hello")
    os.symlink(tmp_path / "real.txt", tmp_path / "alias.txt")
    assert fs.read_file(ws, "alias.txt") == "hello"
    fs.edit_file(ws, "alias.txt", "hello", "bye")
    assert (tmp_path / "real.txt").read_text() == "bye"
    assert os.path.islink(tmp_path / "alias.txt")


def test_symlink_to_outside_directory(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("secret")
    os.symlink(outside, root / "link")
    ws = Workspace(root)
    with pytest.raises(SandboxError):
        fs.read_file(ws, "link/file")
    with pytest.raises(SandboxError):
        fs.write_file(ws, "link/file", "pwned")
    with pytest.raises(SandboxError):
        fs.write_file(ws, "link/new.txt", "pwned")
    with pytest.raises(SandboxError):
        fs.edit_file(ws, "link/file", "secret", "pwned")
    with pytest.raises(SandboxError):
        fs.list_dir(ws, "link")
    assert (outside / "file").read_text() == "secret"
    assert not (outside / "new.txt").exists()


# --- edit_file ----------------------------------------------------------------

def test_edit_binary_file(ws, tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02abc")
    with pytest.raises(ToolError, match="binary"):
        fs.edit_file(ws, "b.bin", "abc", "x")


def test_edit_non_utf8_file(ws, tmp_path):
    (tmp_path / "latin.txt").write_bytes("café".encode("latin-1"))
    with pytest.raises(ToolError, match="UTF-8"):
        fs.edit_file(ws, "latin.txt", "caf", "x")
    with pytest.raises(ToolError, match="UTF-8"):
        fs.read_file(ws, "latin.txt")
    assert (tmp_path / "latin.txt").read_bytes() == "café".encode("latin-1")


def test_edit_old_equals_new_is_refused(ws, tmp_path):
    (tmp_path / "f.txt").write_text("abc")
    with pytest.raises(ToolError, match="identical"):
        fs.edit_file(ws, "f.txt", "b", "b")


def test_edit_empty_old_string_has_clear_message(ws, tmp_path):
    (tmp_path / "f.txt").write_text("abc")
    with pytest.raises(ToolError, match="empty"):
        fs.edit_file(ws, "f.txt", "", "x")


def test_edit_preserves_crlf(ws, tmp_path):
    (tmp_path / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    fs.edit_file(ws, "w.txt", "two", "2")
    assert (tmp_path / "w.txt").read_bytes() == b"one\r\n2\r\nthree\r\n"
    fs.edit_file(ws, "w.txt", "one\r\n2", "1\r\ntwo")
    assert (tmp_path / "w.txt").read_bytes() == b"1\r\ntwo\r\nthree\r\n"


def test_edit_old_string_spanning_lines(ws, tmp_path):
    (tmp_path / "m.py").write_text("def f():\n    return 1\n\ndef g():\n    return 1\n")
    fs.edit_file(ws, "m.py", "def g():\n    return 1", "def g():\n    return 2")
    assert (tmp_path / "m.py").read_text() == "def f():\n    return 1\n\ndef g():\n    return 2\n"


def test_edit_missing_file(ws):
    with pytest.raises(ToolError, match="not found"):
        fs.edit_file(ws, "nope.txt", "a", "b")


# --- no non-ToolError exceptions escape --------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_permission_denied_is_tool_error(ws, tmp_path):
    f = tmp_path / "locked.txt"
    f.write_text("x")
    d = tmp_path / "lockeddir"
    d.mkdir()
    f.chmod(0)
    d.chmod(0)
    try:
        with pytest.raises(ToolError, match="permission denied"):
            fs.read_file(ws, "locked.txt")
        with pytest.raises(ToolError, match="permission denied"):
            fs.write_file(ws, "locked.txt", "y")
        with pytest.raises(ToolError, match="permission denied"):
            fs.edit_file(ws, "locked.txt", "x", "y")
        with pytest.raises(ToolError, match="permission denied"):
            fs.list_dir(ws, "lockeddir")
        with pytest.raises(ToolError, match="permission denied"):
            fs.write_file(ws, "lockeddir/new.txt", "y")
    finally:
        f.chmod(0o644)
        d.chmod(0o755)


@pytest.mark.parametrize("tool,args", [
    (fs.read_file, ()),
    (fs.write_file, ("x",)),
    (fs.edit_file, ("a", "b")),
    (fs.list_dir, ()),
])
def test_very_long_path_is_tool_error(ws, tool, args):
    with pytest.raises(ToolError):
        tool(ws, "x" * 5000, *args)
    with pytest.raises(ToolError):
        tool(ws, "/".join(["y" * 200] * 25), *args)


@pytest.mark.parametrize("tool,args", [
    (fs.read_file, ()),
    (fs.write_file, ("x",)),
    (fs.edit_file, ("a", "b")),
    (fs.list_dir, ()),
])
def test_nul_byte_in_path_is_tool_error(ws, tool, args):
    with pytest.raises(ToolError):
        tool(ws, "a\x00b", *args)


def test_list_dir_missing(ws):
    with pytest.raises(ToolError, match="not a directory"):
        fs.list_dir(ws, "missing")


def test_fs_tools_keep_their_signatures():
    import inspect
    assert list(inspect.signature(fs.edit_file).parameters) == ["ws", "path", "old", "new"]
    assert list(inspect.signature(fs.write_file).parameters) == ["ws", "path", "content"]


# --- workspace root -----------------------------------------------------------

def test_workspace_root_missing(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        Workspace(tmp_path / "nope")


def test_workspace_root_is_a_file(tmp_path):
    (tmp_path / "f").write_text("x")
    with pytest.raises(ValueError, match="not a directory"):
        Workspace(tmp_path / "f")


def test_workspace_root_relative_and_symlinked(tmp_path, monkeypatch):
    (tmp_path / "real").mkdir()
    os.symlink(tmp_path / "real", tmp_path / "alias")
    monkeypatch.chdir(tmp_path)
    ws = Workspace("alias")
    assert ws.root == (tmp_path / "real").resolve()
    fs.write_file(ws, "x.txt", "ok")
    assert (tmp_path / "real/x.txt").read_text() == "ok"


# --- undo -----------------------------------------------------------------------

def test_undo_after_external_modification_keeps_external_version(ws, tmp_path):
    (tmp_path / "a.txt").write_text("v1")
    stack = UndoStack(ws)
    apply(stack, ws, Action("edit_file", {"path": "a.txt", "old": "v1", "new": "v2"}))
    (tmp_path / "a.txt").write_text("user's own edit")
    msg = stack.undo()
    assert (tmp_path / "a.txt").read_text() == "v1"
    assert trashed(tmp_path) == [b"user's own edit"]
    assert "modified" in msg and TRASH_DIR in msg


def test_undo_after_external_modification_of_created_file(ws, tmp_path):
    stack = UndoStack(ws)
    apply(stack, ws, Action("write_file", {"path": "n.txt", "content": "mine"}))
    (tmp_path / "n.txt").write_text("theirs")
    msg = stack.undo()
    assert not (tmp_path / "n.txt").exists()
    assert trashed(tmp_path) == [b"theirs"]
    assert "modified" in msg


def test_undo_unmodified_does_not_claim_modification(ws, tmp_path):
    (tmp_path / "a.txt").write_text("v1")
    stack = UndoStack(ws)
    apply(stack, ws, Action("edit_file", {"path": "a.txt", "old": "v1", "new": "v2"}))
    assert "modified" not in stack.undo()


def test_undo_refuses_when_target_is_now_a_directory(ws, tmp_path):
    stack = UndoStack(ws)
    apply(stack, ws, Action("write_file", {"path": "t", "content": "x"}))
    os.replace(tmp_path / "t", tmp_path / "t.bak")  # user moves it aside...
    (tmp_path / "t").mkdir()                        # ...and makes a directory
    (tmp_path / "t/inner.txt").write_text("keep me")
    msg = stack.undo()
    assert "directory" in msg and "cannot undo" in msg
    assert len(stack) == 1
    assert (tmp_path / "t/inner.txt").read_text() == "keep me"
    assert trashed(tmp_path) == []


def test_undo_refuses_when_parent_is_now_a_file(ws, tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d/a.txt").write_text("v1")
    stack = UndoStack(ws)
    apply(stack, ws, Action("edit_file", {"path": "d/a.txt", "old": "v1", "new": "v2"}))
    os.replace(tmp_path / "d", tmp_path / "d.bak")
    (tmp_path / "d").write_text("now a file")
    msg = stack.undo()
    assert "cannot undo" in msg
    assert len(stack) == 1
    assert (tmp_path / "d").read_text() == "now a file"


def test_undo_edit_of_since_deleted_file_restores(ws, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/a.txt").write_text("v1")
    stack = UndoStack(ws)
    apply(stack, ws, Action("edit_file", {"path": "sub/a.txt", "old": "v1", "new": "v2"}))
    os.replace(tmp_path / "sub", tmp_path / "elsewhere")  # "deleted" from the harness's view
    msg = stack.undo()
    assert (tmp_path / "sub/a.txt").read_text() == "v1"
    assert "undid" in msg
    assert len(stack) == 0


def test_fifty_undos_never_collide(ws, tmp_path):
    stack = UndoStack(ws)
    for i in range(50):
        apply(stack, ws, Action("write_file", {"path": "f.txt", "content": f"v{i}"}))
    for _ in range(50):
        assert "undid" in stack.undo()
    assert not (tmp_path / "f.txt").exists()
    assert trashed(tmp_path) == sorted(f"v{i}".encode() for i in range(50))
    assert "nothing to undo" in stack.undo()


def test_trash_slots_do_not_collide_across_stacks(ws, tmp_path):
    first = UndoStack(ws)
    for i in range(3):
        apply(first, ws, Action("write_file", {"path": "f.txt", "content": f"a{i}"}))
    first.undo()
    second = UndoStack(ws)  # new session, same workspace, first still alive
    apply(second, ws, Action("write_file", {"path": "f.txt", "content": "b0"}))
    second.undo()
    first.undo()
    second.undo()  # nothing left
    first.undo()
    assert trashed(tmp_path) == sorted([b"a2", b"b0", b"a1", b"a0"])
    # a stray non-numeric entry and a file named like a slot must not break it
    (tmp_path / TRASH_DIR / "notes").write_text("x")
    third = UndoStack(ws)
    apply(third, ws, Action("write_file", {"path": "g.txt", "content": "c"}))
    third.undo()
    assert b"c" in trashed(tmp_path)


def test_undo_does_not_write_through_symlinked_away_path(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    ws = Workspace(root)
    (root / "d").mkdir()
    (root / "d/a.txt").write_text("v1")
    stack = UndoStack(ws)
    apply(stack, ws, Action("edit_file", {"path": "d/a.txt", "old": "v1", "new": "v2"}))
    os.replace(root / "d", root / "d.bak")
    os.symlink(outside, root / "d")
    msg = stack.undo()
    assert "cannot undo" in msg
    assert list(outside.iterdir()) == []
    assert len(stack) == 1


def test_snapshot_of_reserved_or_bad_path_is_none(ws):
    stack = UndoStack(ws)
    assert stack.snapshot(Action("write_file", {"path": "../x", "content": "x"})) is None
    assert stack.snapshot(Action("write_file", {"path": "x" * 5000, "content": "x"})) is None
