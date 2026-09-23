"""Edge cases for the shell tool and the macOS sandbox.

Safety: HOME is faked by conftest. Every destructive payload targets only canary files
inside pytest's tmp_path. Denylist tests have "blocks" in their name so the guard below
replaces subprocess and a slipped-through command fails instead of running.
"""
import os
import random
import shlex
import subprocess
import sys
import time

import pytest

from sonic import sandbox
from sonic.errors import ToolError
from sonic.tools import shell
from sonic.tools.shell import MAX_OUTPUT_CHARS, ShellResult, run_shell
from sonic.workspace import Workspace

needs_sandbox = pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
MODES = [pytest.param(True, marks=needs_sandbox, id="sandboxed"), pytest.param(False, id="plain")]


@pytest.fixture(autouse=True)
def never_execute_blocked(request, monkeypatch):
    """Denylist tests must fail, never run, if a dangerous command slips through."""
    if "blocks" not in request.node.name:
        return
    def boom(*a, **k):
        raise AssertionError(f"dangerous command reached subprocess: {a!r}")
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)


def raw(ws, cmd):
    """Run through the sandbox only, bypassing the text denylist, to test the kernel layer."""
    return subprocess.run(sandbox.wrap(cmd, ws), cwd=ws.root, env=sandbox.env(ws),
                          capture_output=True, text=True, timeout=10)


def _lingering(marker: str) -> bool:
    out = subprocess.run(["ps", "-A", "-o", "command="], capture_output=True, text=True).stdout
    return any(marker in line for line in out.splitlines())


def _gone(marker: str, wait: float = 2.0) -> bool:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not _lingering(marker):
            return True
        time.sleep(0.1)
    return not _lingering(marker)


# --- stdin, encoding, output volume -------------------------------------------------------

@pytest.mark.parametrize("sandboxed", MODES)
@pytest.mark.parametrize("cmd", ["cat", 'read x; echo "rc=$?"', 'python3 -c "input()"'])
def test_stdin_readers_return_promptly(tmp_path, sandboxed, cmd):
    start = time.monotonic()
    r = run_shell(Workspace(tmp_path, sandboxed=sandboxed), cmd, timeout=10)
    assert not r.timed_out
    assert time.monotonic() - start < 5


@pytest.mark.parametrize("sandboxed", MODES)
def test_non_utf8_output_is_replaced_not_raised(tmp_path, sandboxed):
    r = run_shell(Workspace(tmp_path, sandboxed=sandboxed),
                  r"printf 'a\377\376b'; printf '\377' >&2")
    assert r.exit_code == 0
    assert r.stdout.startswith("a") and r.stdout.endswith("b") and "�" in r.stdout
    assert "�" in r.stderr


@pytest.mark.parametrize("sandboxed", MODES)
def test_huge_output_is_truncated_quickly(tmp_path, sandboxed):
    start = time.monotonic()
    r = run_shell(Workspace(tmp_path, sandboxed=sandboxed), "yes | head -c 5000000", timeout=20)
    assert time.monotonic() - start < 10
    assert r.exit_code == 0 and not r.timed_out
    assert len(r.stdout) < MAX_OUTPUT_CHARS + 200
    assert "truncated" in r.stdout


def test_endless_output_times_out_with_bounded_capture(tmp_path):
    start = time.monotonic()
    r = run_shell(Workspace(tmp_path, sandboxed=False), "yes", timeout=1)
    assert time.monotonic() - start < 6
    assert r.timed_out
    assert len(r.stdout) < MAX_OUTPUT_CHARS + 200


# --- background children and process groups -----------------------------------------------

@pytest.mark.parametrize("sandboxed", MODES)
def test_background_child_holding_pipe_returns_and_is_killed(tmp_path, sandboxed):
    marker = f"sleep 30.{random.randint(10**6, 10**7)}"
    start = time.monotonic()
    r = run_shell(Workspace(tmp_path, sandboxed=sandboxed), f"{marker} & echo started", timeout=3)
    assert time.monotonic() - start < 6
    assert "started" in r.stdout
    assert _gone(marker), f"{marker!r} outlived run_shell"


def test_timeout_kills_whole_process_group(tmp_path):
    marker = f"sleep 30.{random.randint(10**6, 10**7)}"
    start = time.monotonic()
    r = run_shell(Workspace(tmp_path, sandboxed=False), f"{marker} & sleep 29", timeout=1)
    assert time.monotonic() - start < 6
    assert r.timed_out and r.exit_code is None
    assert _gone(marker)


# --- bad commands ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["", "   ", "\n\t  \n"])
def test_empty_command_is_tool_error(tmp_path, cmd):
    with pytest.raises(ToolError, match="(?i)empty"):
        run_shell(Workspace(tmp_path), cmd)


@pytest.mark.parametrize("cmd", [None, 123, b"echo hi", ["echo", "hi"]])
def test_non_string_command_is_tool_error(tmp_path, cmd):
    with pytest.raises(ToolError):
        run_shell(Workspace(tmp_path), cmd)


def test_nul_byte_in_command_is_tool_error(tmp_path):
    with pytest.raises(ToolError):
        run_shell(Workspace(tmp_path), "echo a\x00b")


@pytest.mark.parametrize("sandboxed", MODES)
def test_100kb_command_runs(tmp_path, sandboxed):
    start = time.monotonic()
    r = run_shell(Workspace(tmp_path, sandboxed=sandboxed), "echo " + "a" * 100_000, timeout=20)
    assert time.monotonic() - start < 10
    assert r.exit_code == 0 and r.stdout.startswith("aaaa")


@pytest.mark.parametrize("body", [
    "a" * 100_000, "a:" * 50_000, "x() " * 25_000, "find . " * 15_000,
    "base64 -d " * 10_000, "dd " * 30_000, "`" * 100_000, '"' * 100_001, "$(" * 50_000,
])
def test_long_pathological_commands_finish_fast(tmp_path, body):
    start = time.monotonic()
    try:
        res = run_shell(Workspace(tmp_path, sandboxed=False), "echo " + body, timeout=10)
        assert isinstance(res, ShellResult)
    except ToolError:
        pass
    assert time.monotonic() - start < 10


@pytest.mark.parametrize("sandboxed", MODES)
def test_oversized_command_is_tool_error_not_oserror(tmp_path, sandboxed):
    with pytest.raises(ToolError):
        run_shell(Workspace(tmp_path, sandboxed=sandboxed), "echo " + "a" * 3_000_000)


def test_missing_workspace_root_is_tool_error(tmp_path):
    with pytest.raises(ToolError):
        run_shell(Workspace(tmp_path / "gone", sandboxed=False), "echo hi")


def test_undecodable_surrogates_are_tool_error_or_run(tmp_path):
    try:
        run_shell(Workspace(tmp_path, sandboxed=False), "echo \udcff")
    except ToolError:
        pass


# --- timeout argument -----------------------------------------------------------------------

@pytest.mark.parametrize("timeout", [0, -1, -0.5, float("nan"), float("inf"), "abc", "", True, [5]])
def test_bad_timeout_is_tool_error(tmp_path, timeout):
    with pytest.raises(ToolError, match="(?i)timeout"):
        run_shell(Workspace(tmp_path, sandboxed=False), "echo hi", timeout=timeout)


@pytest.mark.parametrize("timeout", ["5", " 2.5 ", 3, 1.5, None])
def test_numeric_like_timeout_is_coerced(tmp_path, timeout):
    r = run_shell(Workspace(tmp_path, sandboxed=False), "echo hi", timeout=timeout)
    assert r.stdout.strip() == "hi"


# --- sandbox-exec failures ------------------------------------------------------------------

def test_missing_sandbox_exec_is_tool_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(tmp_path / "no-such-sandbox-exec"))
    with pytest.raises(ToolError, match="(?i)sandbox"):
        run_shell(Workspace(tmp_path), "echo hi")


def test_sandbox_exec_that_cannot_start_is_tool_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(tmp_path / "no-such-sandbox-exec"))
    with pytest.raises(ToolError, match="(?i)sandbox"):
        run_shell(Workspace(tmp_path), "echo hi")


def test_non_executable_sandbox_exec_is_tool_error(tmp_path, monkeypatch):
    fake = tmp_path / "fake-sandbox-exec"
    fake.write_text("not a program")
    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(fake))
    with pytest.raises(ToolError, match="(?i)sandbox"):
        run_shell(Workspace(tmp_path), "echo hi")


@needs_sandbox
def test_sandbox_profile_rejected_is_tool_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "profile", lambda ws: "(version 1) (bogus")
    with pytest.raises(ToolError, match="(?i)sandbox"):
        run_shell(Workspace(tmp_path), "echo hi")


# --- sandbox profile escaping ---------------------------------------------------------------

@needs_sandbox
@pytest.mark.parametrize("name", [
    "with space", 'dq"uote', "back\\slash", "ünïcødé ✓ 日本", 'mix "a\\b" (c); d',
    "trail\\", 'end"', "new\nline",
])
def test_sandbox_escaping_enforces_policy_for_odd_workspace_paths(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    (tmp_path / "outside").mkdir()
    ws = Workspace(root)
    keep = root / "keep.txt"
    keep.write_text("keep")
    canary = tmp_path / "outside" / "canary.txt"
    canary.write_text("canary")
    r = raw(ws, "echo new > out.txt && echo ok")
    assert r.returncode == 0, r.stderr
    assert (root / "out.txt").read_text() == "new\n"
    raw(ws, "rm -f keep.txt")
    assert keep.read_text() == "keep"
    raw(ws, f"rm -f {shlex.quote(str(canary))}")
    assert canary.read_text() == "canary"
    raw(ws, f"echo pwned > {shlex.quote(str(canary))}")
    assert canary.read_text() == "canary"


@needs_sandbox
def test_sandbox_escaping_does_not_widen_to_sibling_prefix(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    sibling = tmp_path / "ws2"
    sibling.mkdir()
    ws = Workspace(root)
    raw(ws, f"echo pwned > '{sibling}/f.txt'")
    assert not (sibling / "f.txt").exists()


# --- .sonic is harness-owned ----------------------------------------------------------------

@pytest.fixture
def sonic_ws(tmp_path):
    root = tmp_path / "ws"
    (root / ".sonic" / "context").mkdir(parents=True)
    graph = root / ".sonic" / "context" / "graph.json"
    graph.write_text("canary")
    return Workspace(root), graph


@needs_sandbox
@pytest.mark.parametrize("cmd", [
    "echo pwned > .sonic/context/graph.json",
    "echo pwned >> .sonic/context/graph.json",
    "echo pwned > .SONIC/context/graph.json",
    "mkdir .sonic/trash",
    "mv .sonic moved",
    "mv .sonic/context/graph.json .sonic/context/g2.json",
    "ln -s .sonic link && echo pwned > link/context/graph.json",
    "ln .sonic/context/graph.json hard && echo pwned > hard",
    "cd .sonic/context && echo pwned > graph.json",
    "python3 -c \"open('.sonic/context/graph.json', 'w').write('pwned')\"",
    ": > .sonic/context/graph.json",
    "truncate -s 0 .sonic/context/graph.json",
    "touch .sonic/new.txt",
])
def test_sandbox_denies_writes_into_dot_sonic(sonic_ws, cmd):
    ws, graph = sonic_ws
    raw(ws, cmd)
    assert graph.read_text() == "canary"
    assert sorted(p.name for p in (ws.root / ".sonic").iterdir()) == ["context"]
    assert [p.name for p in (ws.root / ".sonic" / "context").iterdir()] == ["graph.json"]


@needs_sandbox
def test_sandbox_denies_creating_dot_sonic(tmp_path):
    ws = Workspace(tmp_path)
    raw(ws, "mkdir .sonic; mkdir -p .sonic/context && echo x > .sonic/context/graph.json")
    assert not (tmp_path / ".sonic").exists()


@needs_sandbox
def test_rest_of_workspace_stays_writable_next_to_dot_sonic(sonic_ws):
    ws, graph = sonic_ws
    r = run_shell(ws, "mkdir -p src/.sonicx && echo ok > src/a.txt && echo ok > .sonicrc "
                      "&& echo ok > src/.sonicx/b && mkdir -p sub/.sonic && echo ok > sub/.sonic/c "
                      "&& cat src/a.txt")
    assert r.exit_code == 0, r.stderr
    assert (ws.root / "src" / "a.txt").read_text() == "ok\n"
    assert (ws.root / ".sonicrc").exists() and (ws.root / "sub" / ".sonic" / "c").exists()
    assert graph.read_text() == "canary"
    r = run_shell(ws, "echo pwned > .sonic/context/graph.json")
    assert r.exit_code != 0
    assert graph.read_text() == "canary"


@needs_sandbox
def test_reading_dot_sonic_is_still_allowed(sonic_ws):
    ws, _ = sonic_ws
    assert run_shell(ws, "cat .sonic/context/graph.json").stdout == "canary"


# --- denylist gaps and false positives ------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "git rm file.txt", "git rm -r --cached x", "git -C . rm f", "/usr/bin/git rm f",
    "command git rm f", "ls && git rm f", "git --no-pager rm -f f", "git -c a.b=c rm f",
    "echo ok; git rm f",
])
def test_run_shell_blocks_git_rm(tmp_path, cmd):
    with pytest.raises(ToolError, match="(?i)blocked"):
        run_shell(Workspace(tmp_path), cmd)


@pytest.mark.parametrize("cmd", [
    'echo "a" ; rm b',
    "echo 'x' && rm b",
    '"rm" b',
    "'rm' -f b",
    'echo "$(rm b)"',
    "echo \"a`rm b`\"",
    "echo `rm b`",
    "echo hi # don't\nrm b\necho 'ok'",
    "cat <<EOF\nit's\nEOF\nrm b",
    "echo $'it\\'s'; rm b",
    'echo "unterminated ; rm b',
    "echo 'a' 'b';rm b",
    'echo "a\\"; rm b"; rm c',
    "echo a\\;; rm b",
    'bash -c "echo a; rm b"',
    "sh -c 'echo \"x; y\"; rm b'",
    "eval \"echo a; rm b\"",
])
def test_run_shell_blocks_real_separators_around_quotes(tmp_path, cmd):
    with pytest.raises(ToolError, match="(?i)blocked"):
        run_shell(Workspace(tmp_path), cmd)


@pytest.mark.parametrize("cmd", [
    'git commit -m "fix; rm later"',
    "git commit -m 'drop x && rm y later'",
    'grep -n "foo | rm" file.txt',
    "echo 'a | unlink b'",
    'printf "%s\\n" "x; rmdir y"',
    'echo "it\'s; rm b"',
])
def test_separators_inside_quotes_are_not_blocked(cmd):
    shell._check_denylist(cmd)


@pytest.mark.parametrize("sandboxed", MODES)
def test_quoted_separators_run(tmp_path, sandboxed):
    ws = Workspace(tmp_path, sandboxed=sandboxed)
    assert run_shell(ws, 'echo "a; rm b"').stdout == "a; rm b\n"
    assert run_shell(ws, "echo 'x && unlink y'").stdout == "x && unlink y\n"


@pytest.mark.parametrize("sandboxed", MODES)
def test_truncate_is_not_deletion_and_is_allowed(tmp_path, sandboxed):
    """truncate empties a file but keeps it; the undo/trash layer is responsible for content."""
    f = tmp_path / "f.txt"
    f.write_text("data")
    r = run_shell(Workspace(tmp_path, sandboxed=sandboxed), "truncate -s 0 f.txt")
    assert r.exit_code == 0, r.stderr
    assert f.exists() and f.read_text() == ""


# --- only ToolError may escape ----------------------------------------------------------------

@pytest.mark.parametrize("cmd,timeout", [
    ("echo hi", "1e400"), ("echo hi", 1e-9), ("exit 300", 5), ("kill -9 $$", 5),
    ("kill -TERM 0", 5), ("exec 1>&-; exec 2>&-; sleep 0.2", 5), ("ulimit -f 0; echo hi", 5),
])
def test_only_tool_error_escapes(tmp_path, cmd, timeout):
    try:
        r = run_shell(Workspace(tmp_path, sandboxed=False), cmd, timeout=timeout)
    except ToolError:
        return
    assert isinstance(r, ShellResult)
    str(r)
