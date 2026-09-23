"""Context graph (second brain)."""
from sonic.context import GRAPH_PATH, ContextGraph
from sonic.planner import Action, Step
from sonic.workspace import Workspace


def ids(nodes):
    return [n["id"] for n in nodes]


# 38
def test_graph_upserts_dedupes_and_persists(tmp_path):
    ws = Workspace(tmp_path)
    g = ContextGraph(ws)
    a = g.add_node("file", "a.py", summary="entry point")
    assert a == "file:a.py"
    g.add_node("file", "a.py", tags=["core"])
    b = g.add_node("file", "b.py")
    g.add_edge(a, b, "imports")
    g.add_edge(a, b, "imports")
    g.save()
    assert (tmp_path / GRAPH_PATH).exists()

    g2 = ContextGraph(ws)
    n = g2.node("file:a.py")
    assert n["hits"] == 2 and n["summary"] == "entry point" and "core" in n["tags"]
    edges = g2.to_json()["edges"]
    assert len(edges) == 1 and edges[0]["weight"] == 2
    assert ids(g2.neighbors("file:b.py")) == ["file:a.py"]


# 39
def test_record_step_captures_files_commands_symbols_imports(tmp_path):
    ws = Workspace(tmp_path)
    (tmp_path / "util.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "app.py").write_text("import util\n\nclass App:\n    pass\n\ndef main():\n    util.helper()\n")
    g = ContextGraph(ws)
    t = g.start_task("edit app then run it")
    g.record_step(t, Step(Action("edit_file", {"path": "app.py", "old": "a", "new": "b"}), True, "edited app.py"))
    g.record_step(t, Step(Action("read_file", {"path": "missing.py"}), False, "error"))
    g.record_step(t, Step(Action("run_shell", {"command": "python3 app.py"}), True, "exit 0"))
    near = set(ids(g.neighbors(t)))
    assert "file:app.py" in near and "file:missing.py" not in near
    cmd = [n for n in g.neighbors(t) if n["type"] == "command"][0]
    assert "file:app.py" in ids(g.neighbors(cmd["id"]))
    app_near = set(ids(g.neighbors("file:app.py")))
    assert {"symbol:app.py::App", "symbol:app.py::main", "file:util.py"} <= app_near
    rels = {(e["src"], e["dst"], e["rel"]) for e in g.to_json()["edges"]}
    assert (t, "file:app.py", "edited") in rels and ("file:app.py", "file:util.py", "imports") in rels
    assert g.start_task("second") != t


# 40
def test_remember_links_files_and_tags_and_forget_archives(tmp_path):
    ws = Workspace(tmp_path)
    (tmp_path / "auth.py").write_text("x = 1\n")
    g = ContextGraph(ws)
    note = g.remember("auth.py uses JWT; tokens expire after 1h #security #auth")
    near = set(ids(g.neighbors(note)))
    assert {"file:auth.py", "topic:security", "topic:auth"} <= near
    assert g.forget(note) is True
    assert note not in ids(g.relevant("JWT tokens"))
    assert any(n["id"] == note and n["archived"] for n in g.to_json()["nodes"])


# 41
def test_relevant_ranks_keyword_hits_and_pulls_in_neighbours(tmp_path):
    ws = Workspace(tmp_path)
    (tmp_path / "login.py").write_text("x = 1\n")
    g = ContextGraph(ws)
    note = g.remember("login.py handles OAuth with Google")
    g.remember("the build uses make and a Dockerfile")
    g.add_node("file", "unrelated.txt")
    ranked = ids(g.relevant("how does oauth work", k=3))
    assert ranked[0] == note
    assert "file:login.py" in ranked
    assert "file:unrelated.txt" not in ranked


# 42
def test_render_context_is_readable_and_within_budget(tmp_path):
    g = ContextGraph(Workspace(tmp_path))
    for i in range(50):
        g.remember(f"deploy fact number {i} about the staging deploy pipeline")
    text = g.render_context("deploy pipeline", budget_chars=500)
    assert 0 < len(text) <= 500
    assert "deploy" in text
    assert g.render_context("zzzz nothing matches") == ""
