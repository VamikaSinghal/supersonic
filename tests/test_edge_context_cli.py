"""Edge cases: context graph persistence/capture, session log, and the CLI/REPL."""
import io
import json
import os
import shlex
import socket
import stat
import subprocess
import sys
import time
from contextlib import contextmanager

import pytest

import sonic.__main__ as cli
from sonic.context import GRAPH_PATH, ContextGraph
from sonic.log import SessionLog
from sonic.planner import Action, Step
from sonic.workspace import Workspace


def graph_file(tmp_path):
    return tmp_path / GRAPH_PATH


def write_graph(tmp_path, content: str | bytes):
    p = graph_file(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    return p


def backups(tmp_path):
    return sorted(p.name for p in graph_file(tmp_path).parent.iterdir()
                  if p.name != "graph.json" and not p.name.startswith(".graph-"))


@contextmanager
def readonly(path):
    """chmod a directory 0o500 for the duration; always restores it."""
    old = stat.S_IMODE(path.stat().st_mode)
    os.chmod(path, 0o500)
    try:
        yield
    finally:
        os.chmod(path, old)


def ok_step(tool, **args):
    return Step(Action(tool, args), True, "ok")


# --- context graph: loading corrupt / foreign files ---

@pytest.mark.parametrize("content", [
    "{not json",
    "[1, 2, 3]",
    '"just a string"',
    '{"foo": 1}',
    '{"nodes": 5, "edges": []}',
    b"\xff\xfe\x00garbage",
])
def test_corrupt_graph_starts_empty_and_keeps_the_file(tmp_path, content, capsys):
    write_graph(tmp_path, content)
    g = ContextGraph(Workspace(tmp_path))
    assert g.to_json() == {"nodes": [], "edges": []}
    kept = backups(tmp_path)
    assert len(kept) == 1 and "corrupt" in kept[0]
    raw = (graph_file(tmp_path).parent / kept[0]).read_bytes()
    assert raw == (content if isinstance(content, bytes) else content.encode())
    assert "graph" in capsys.readouterr().err.lower()
    # still usable and saves cleanly
    g.remember("fresh start #ok")
    g.save()
    assert json.loads(graph_file(tmp_path).read_text())["nodes"]


def test_corrupt_graph_backups_never_overwrite_each_other(tmp_path):
    for i in range(3):
        write_graph(tmp_path, f"broken {i}")
        ContextGraph(Workspace(tmp_path))
    kept = backups(tmp_path)
    assert len(kept) == 3
    contents = {(graph_file(tmp_path).parent / k).read_text() for k in kept}
    assert contents == {"broken 0", "broken 1", "broken 2"}


def test_graph_with_missing_edges_key_and_partial_nodes_is_repaired(tmp_path):
    write_graph(tmp_path, json.dumps({
        "version": 1,
        "task_counter": "not a number",
        "nodes": [
            {"id": "note:abc", "summary": "jwt tokens live in auth"},   # no type/key/label/...
            {"id": "file:a.py", "type": "file", "tags": "oops", "data": [], "hits": "x"},
            {"id": ["unhashable"]},
            {"no_id": True},
            "not a dict",
            {"id": "note:bad", "type": "note", "data": {"pending_imports": "zzz"}},
        ],
    }))
    g = ContextGraph(Workspace(tmp_path))
    got = {n["id"] for n in g.to_json()["nodes"]}
    assert got == {"note:abc", "file:a.py", "note:bad"}
    n = g.node("note:abc")
    assert n["type"] == "note" and n["key"] == "abc" and isinstance(n["tags"], list)
    assert [x["id"] for x in g.relevant("jwt")] == ["note:abc"]
    g.add_node("file", "a.py", tags=["core"])
    assert g.node("file:a.py")["tags"] == ["core"]
    g.start_task("hello")
    g.save()
    assert json.loads(graph_file(tmp_path).read_text())["nodes"]


def test_graph_with_bad_edges_skips_them(tmp_path):
    write_graph(tmp_path, json.dumps({
        "version": 1, "nodes": [{"id": "file:a", "type": "file", "key": "a"},
                                {"id": "file:b", "type": "file", "key": "b"}],
        "edges": [{"src": "file:a", "dst": "file:b", "rel": "imports", "weight": "heavy"},
                  {"src": ["x"], "dst": "file:b", "rel": "imports"},
                  {"src": "file:a", "dst": "file:missing", "rel": "imports"},
                  {"src": "file:a", "dst": "file:b"},
                  7],
    }))
    g = ContextGraph(Workspace(tmp_path))
    edges = g.to_json()["edges"]
    assert len(edges) == 1 and edges[0]["weight"] == 1
    assert [n["id"] for n in g.neighbors("file:a")] == ["file:b"]


def test_graph_from_future_version_loads_what_it_can_and_backs_up(tmp_path, capsys):
    original = json.dumps({"version": 99, "task_counter": 3, "shiny": {"x": 1},
                           "nodes": [{"id": "note:k", "type": "note", "key": "k",
                                      "label": "future note", "summary": "future note"}],
                           "edges": []})
    write_graph(tmp_path, original)
    g = ContextGraph(Workspace(tmp_path))
    assert g.node("note:k")["label"] == "future note"
    assert "version" in capsys.readouterr().err.lower()
    kept = backups(tmp_path)
    assert len(kept) == 1
    assert (graph_file(tmp_path).parent / kept[0]).read_text() == original
    g.save()
    assert json.loads(graph_file(tmp_path).read_text())["version"] == 1


# --- context graph: concurrent writers ---

def test_two_graphs_saving_alternately_last_writer_wins_and_file_stays_valid(tmp_path):
    ws = Workspace(tmp_path)
    g1, g2 = ContextGraph(ws), ContextGraph(ws)
    for i in range(15):
        g1.remember(f"first writer note {i}")
        g1.save()
        assert json.loads(graph_file(tmp_path).read_text())
        g2.remember(f"second writer note {i}")
        g2.save()
        assert json.loads(graph_file(tmp_path).read_text())
    labels = {n["label"] for n in json.loads(graph_file(tmp_path).read_text())["nodes"]}
    assert "second writer note 14" in labels and "first writer note 14" not in labels
    leftovers = [p for p in graph_file(tmp_path).parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# --- context graph: capture of hostile files ---

@pytest.mark.parametrize("content", [
    b"def broken(:\n    pass\n",
    b"\xff\xfe\xfa not utf8 def f(): pass\n",
    b"x = " + b"(" * 200_000 + b"\n",
    b"def ok():\n    pass\n\x00\n",
])
def test_record_step_survives_bad_python(tmp_path, content):
    (tmp_path / "bad.py").write_bytes(content)
    g = ContextGraph(Workspace(tmp_path))
    t = g.start_task("write bad.py")
    g.record_step(t, ok_step("write_file", path="bad.py", content="..."))
    assert g.node("file:bad.py") is not None
    g.save()


def test_record_step_skips_parsing_huge_python_files(tmp_path):
    body = "def first():\n    pass\n" + "x = 1\n" * 350_000     # ~2 MB
    (tmp_path / "big.py").write_text(body)
    assert (tmp_path / "big.py").stat().st_size > 2_000_000
    g = ContextGraph(Workspace(tmp_path))
    t = g.start_task("write big.py")
    start = time.monotonic()
    g.record_step(t, ok_step("write_file", path="big.py", content="..."))
    assert time.monotonic() - start < 1.0
    assert g.node("file:big.py") is not None
    assert g.node("symbol:big.py::first") is None          # over the parse cap: not indexed


@pytest.mark.parametrize("args", [
    {}, {"path": ""}, {"path": None}, {"path": "../outside.txt"}, {"path": "/etc/passwd"},
    {"path": "a\x00b"}, {"path": ".sonic/context/graph.json"}, {"path": 12},
])
def test_record_step_with_missing_or_escaping_path_adds_no_file(tmp_path, args):
    g = ContextGraph(Workspace(tmp_path))
    t = g.start_task("x")
    g.record_step(t, Step(Action("write_file", args), True, "ok"))
    assert [n["id"] for n in g.to_json()["nodes"]] == [t]


def test_record_step_with_none_args_and_unknown_task(tmp_path):
    g = ContextGraph(Workspace(tmp_path))
    g.record_step("task:404", Step(Action("run_shell", None), True, "ok"))
    g.record_step("task:404", Step(Action("mystery_tool", {"path": "a"}), True, "ok"))
    assert g.to_json()["nodes"] == []


# --- context graph: remember / retrieval edge cases ---

@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_remember_empty_text_is_rejected(tmp_path, text):
    g = ContextGraph(Workspace(tmp_path))
    with pytest.raises(ValueError):
        g.remember(text)
    assert g.to_json()["nodes"] == []


def test_remember_unicode_and_punctuated_hashtags(tmp_path):
    g = ContextGraph(Workspace(tmp_path))
    nid = g.remember("coffee notes #café and #日本, see #auth. also #auth! and #auth- done")
    assert g.node(nid)["tags"] == ["café", "日本", "auth"]
    topics = {n["id"] for n in g.neighbors(nid)}
    assert topics == {"topic:café", "topic:日本", "topic:auth"}
    assert nid in [n["id"] for n in g.relevant("café")]
    assert nid in [n["id"] for n in g.relevant("日本")]


def test_remember_links_quoted_path_with_spaces(tmp_path):
    (tmp_path / "my notes.md").write_text("hi")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "long name.txt").write_text("hi")
    g = ContextGraph(Workspace(tmp_path))
    nid = g.remember("read \"my notes.md\" and 'docs/long name.txt' first")
    linked = {n["id"] for n in g.neighbors(nid)}
    assert {"file:my notes.md", "file:docs/long name.txt"} <= linked


def test_remember_very_long_text(tmp_path):
    g = ContextGraph(Workspace(tmp_path))
    text = ("lorem ipsum a.b/c.d #tag " * 2100)[:50_000] + " unique_marker_word"
    start = time.monotonic()
    nid = g.remember(text)
    assert time.monotonic() - start < 3.0
    assert len(g.node(nid)["label"]) <= 60
    assert nid in [n["id"] for n in g.relevant("unique_marker_word")]
    g.save()
    assert ContextGraph(Workspace(tmp_path)).node(nid) is not None


@pytest.mark.parametrize("query", ["", "   ", "the and of to", "#", "!!!"])
def test_relevant_with_empty_or_stopword_query(tmp_path, query):
    g = ContextGraph(Workspace(tmp_path))
    g.remember("the auth module uses jwt")
    assert g.relevant(query) == []
    assert g.render_context(query) == ""


def test_unknown_ids(tmp_path):
    g = ContextGraph(Workspace(tmp_path))
    assert g.forget("note:nope") is False
    assert g.neighbors("note:nope") == []
    assert g.neighbors("note:nope", depth=5) == []
    assert g.node("note:nope") is None


# --- context graph: unwritable storage ---

def test_unwritable_graph_dir_warns_once_and_never_raises(tmp_path, capsys):
    ws = Workspace(tmp_path)
    g = ContextGraph(ws)
    g.remember("first")
    g.save()
    ctx = graph_file(tmp_path).parent
    with readonly(ctx):
        g.remember("second")
        g.save()
        g.remember("third")
        g.save()
        err = capsys.readouterr().err
        assert err.lower().count("warning") == 1 and "graph" in err.lower()
        assert [p.name for p in ctx.iterdir()] == ["graph.json"]    # no stray temp files
    # permissions back: saving works again
    g.save()
    labels = {n["label"] for n in json.loads(graph_file(tmp_path).read_text())["nodes"]}
    assert {"first", "second", "third"} <= labels


def test_unwritable_graph_dir_in_repl(tmp_path):
    ctx = tmp_path / ".sonic" / "context"
    ctx.mkdir(parents=True)
    with readonly(ctx):
        r = run_sonic(tmp_path, "/remember hello #x\ncreate a.txt with hi\n/brain\n", "--yolo")
    assert r.returncode == 0, r.stderr
    assert "remembered note:" in r.stdout and "completed 1 step" in r.stdout
    assert "Traceback" not in r.stderr and r.stderr.lower().count("warning") == 1


# --- session log ---

def test_unwritable_sessions_dir_warns_and_repl_keeps_working(tmp_path):
    sessions = tmp_path / ".sonic" / "sessions"
    sessions.mkdir(parents=True)
    with readonly(sessions):
        r = run_sonic(tmp_path, "create a.txt with hi\n/log\nread a.txt\n", "--yolo")
    assert r.returncode == 0
    assert (tmp_path / "a.txt").read_text() == "hi"
    assert "session log disabled" in r.stderr and r.stderr.count("warning") == 1
    assert "no steps yet" in r.stdout and "Traceback" not in r.stderr


def test_recent_skips_malformed_lines(tmp_path):
    log = SessionLog(Workspace(tmp_path))
    log.step(Step(Action("write_file", {"path": "good.txt", "content": "x"}), True, "wrote 1 bytes"))
    with log.path.open("ab") as f:
        f.write(b"{not json\n")
        f.write(b"[1, 2]\n")
        f.write(b"\xff\xfe\xfd\n")
        f.write(b'{"event": "step", "args": "str", "observation": 5, "tool": null}\n')
        f.write(b'{"event": "step", "args": {"path": ["x"]}, "observation": ["a"]}\n')
        f.write(b"\n")
    log.step(Step(Action("run_shell", {"command": "ls"}), False, "boom\nmore"))
    out = log.recent()
    assert "✓ write_file good.txt" in out and "✗ run_shell: ls → boom" in out


def test_log_step_with_none_args_and_odd_values(tmp_path):
    log = SessionLog(Workspace(tmp_path))
    log.step(Step(Action("run_shell", None), True, "ok"))
    log.step(Step(Action("write_file", {"path": "a", "content": b"bytes"}), True, "ok"))
    log.instruction("x" * 100_000)
    lines = log.path.read_text().splitlines()
    assert len(lines) == 3 and all(json.loads(line) for line in lines)


# --- CLI / REPL ---

def run_sonic(root, stdin, *flags, timeout=30):
    return subprocess.run(
        [sys.executable, "-m", "sonic", "--root", str(root), *flags],
        input=stdin, capture_output=True, text=True, timeout=timeout,
    )


def assert_one_line_error(r):
    assert r.returncode == 2
    assert "Traceback" not in r.stderr
    assert len(r.stderr.strip().splitlines()) == 1, r.stderr


def test_root_that_does_not_exist(tmp_path):
    missing = tmp_path / "nope"
    r = run_sonic(missing, "")
    assert_one_line_error(r)
    assert "nope" in r.stderr
    assert not missing.exists()                      # never created


def test_root_that_is_a_file(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    r = run_sonic(f, "")
    assert_one_line_error(r)
    assert f.read_text() == "x"


@pytest.mark.parametrize("flags", [
    ("--max-steps", "0"), ("--max-steps", "-3"), ("--max-steps", "many"),
    ("--port", "0"), ("--port", "70000"), ("--port", "-1"),
])
def test_bad_numeric_flags_are_argparse_errors(tmp_path, flags):
    r = run_sonic(tmp_path, "", *flags)
    assert r.returncode == 2 and "error" in r.stderr and "Traceback" not in r.stderr


def test_web_port_in_use(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        r = run_sonic(tmp_path, "", "--web", "--port", str(port), timeout=15)
    assert_one_line_error(r)
    assert str(port) in r.stderr


def test_eof_during_approval_denies_and_exits_cleanly(tmp_path):
    r = run_sonic(tmp_path, "create a.txt with hi then create b.txt with yo\n")
    assert r.returncode == 0 and "Traceback" not in r.stderr
    assert "denied by user" in r.stdout
    assert not (tmp_path / "a.txt").exists() and not (tmp_path / "b.txt").exists()


def test_very_long_input_lines(tmp_path):
    content = "x" * 100_000
    junk = "blah " * 20_000
    r = run_sonic(tmp_path, f"create big.txt with {content}\n{junk}\n/remember {junk}\n/context blah\n",
                  "--yolo", timeout=60)
    assert r.returncode == 0 and "Traceback" not in r.stderr
    assert (tmp_path / "big.txt").read_text() == content
    assert "didn't understand" in r.stdout and "remembered note:" in r.stdout


def test_slash_command_variants(tmp_path):
    r = run_sonic(tmp_path, "/remember    spaced   out #tidy\n/UNDO\n/remember\n/remember    \n"
                            "/context\n/context    \n/foo\n/Brain\n/remember\tvia tab\n/HELP\n/QUIT\n"
                            "create never.txt with x\n", "--yolo")
    out = r.stdout
    assert r.returncode == 0 and "Traceback" not in r.stderr
    assert out.count("remembered note:") == 2
    assert "nothing to undo" in out.lower()
    assert out.count("usage: /remember") == 2
    assert out.count("(nothing relevant yet)") == 2
    assert "unknown command /foo" in out
    assert "nodes (" in out and "Slash commands" in out
    assert not (tmp_path / "never.txt").exists()        # /QUIT stopped the session


class ExplodingPlanner:
    def __init__(self):
        self.calls = 0

    def next_action(self, instruction, history):
        self.calls += 1
        raise RuntimeError("model went away")


def test_planner_raising_mid_run_prints_error_and_continues(tmp_path, monkeypatch, capsys):
    planner = ExplodingPlanner()
    monkeypatch.setattr(cli, "make_planner", lambda kind="stub", context=None: planner)
    monkeypatch.setattr(sys, "stdin", io.StringIO("do something\ndo it again\n/brain\n"))
    code = cli.main(["--root", str(tmp_path), "--yolo"])
    out = capsys.readouterr().out
    assert code == 0 and planner.calls == 2
    assert out.count("model went away") == 2
    assert "nodes (" in out                     # REPL kept going to /brain
    events = [json.loads(line) for p in (tmp_path / ".sonic" / "sessions").glob("*.jsonl")
              for line in p.read_text().splitlines()]
    assert [e["event"] for e in events].count("final") == 2


def test_broken_pipe_does_not_dump_a_traceback(tmp_path):
    script = tmp_path / "in.txt"
    script.write_text("/help\n" * 3000)
    cmd = (f"{shlex.quote(sys.executable)} -m sonic --root {shlex.quote(str(tmp_path))} "
           f"< {shlex.quote(str(script))} | head -1")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    assert r.stdout.startswith("sonic")
    assert "Traceback" not in r.stderr and "BrokenPipe" not in r.stderr, r.stderr
