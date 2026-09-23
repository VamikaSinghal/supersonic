"""Local web UI API (the page itself is checked manually)."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from sonic.planner import StubPlanner
from sonic.web import create_app
from sonic.workspace import Workspace


@pytest.fixture
def app(tmp_path):
    app = create_app(Workspace(tmp_path), StubPlanner())
    threading.Thread(target=app.serve_forever, daemon=True).start()
    yield app
    app.shutdown()


def call(app, method, path, body=None, token=True, host=None):
    req = urllib.request.Request(app.url.rstrip("/") + path, method=method,
                                 data=None if body is None else json.dumps(body).encode())
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Sonic-Token", app.token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        return e.code, None


def wait_for(app, kind, timeout=5.0):
    deadline, after = time.time() + timeout, 0
    seen = []
    while time.time() < deadline:
        _, data = call(app, "GET", f"/api/events?after={after}")
        seen += data["events"]
        after = data["next"]
        hits = [e for e in seen if e["type"] == kind]
        if hits:
            return hits[-1], seen
        time.sleep(0.05)
    raise AssertionError(f"no {kind} event; saw {seen}")


# 35
def test_web_serves_page_and_state_to_localhost_only(app):
    status, page = call(app, "GET", "/")
    assert status == 200 and "<title>" in page and app.token in page
    status, state = call(app, "GET", "/api/state")
    assert status == 200 and state["sandbox"] is True and state["busy"] is False
    assert call(app, "GET", "/api/state", host="evil.example.com")[0] == 403
    assert call(app, "POST", "/api/run", {"instruction": "list"}, token=False)[0] == 403


# 36
def test_web_run_with_approval_shows_diff_then_executes(app, tmp_path):
    assert call(app, "POST", "/api/run", {"instruction": "create a.txt with hi"})[0] == 202
    req, _ = wait_for(app, "approval_request")
    assert req["tool"] == "write_file" and "+hi" in req["diff"]
    assert call(app, "POST", "/api/run", {"instruction": "list"})[0] == 409   # busy
    assert call(app, "POST", "/api/approve", {"id": req["id"], "decision": "y"})[0] == 200
    final, events = wait_for(app, "final")
    assert (tmp_path / "a.txt").read_text() == "hi"
    assert any(e["type"] == "step" and e["ok"] for e in events)


# 37
def test_web_deny_then_undo(app, tmp_path):
    call(app, "POST", "/api/run", {"instruction": "create a.txt with hi"})
    req, _ = wait_for(app, "approval_request")
    call(app, "POST", "/api/approve", {"id": req["id"], "decision": "n"})
    _, events = wait_for(app, "final")
    assert not (tmp_path / "a.txt").exists()
    assert any(e["type"] == "step" and "denied" in e["observation"] for e in events)

    call(app, "POST", "/api/run", {"instruction": "create b.txt with yo"})
    req, _ = wait_for(app, "approval_request")
    call(app, "POST", "/api/approve", {"id": req["id"], "decision": "a"})
    time.sleep(0.3)
    assert (tmp_path / "b.txt").read_text() == "yo"
    status, body = call(app, "POST", "/api/undo")
    assert status == 200 and "b.txt" in body["message"]
    assert not (tmp_path / "b.txt").exists() and (tmp_path / ".sonic" / "trash").exists()


# 46
def test_web_brain_api_graph_remember_context(app, tmp_path):
    (tmp_path / "auth.py").write_text("x = 1\n")
    status, body = call(app, "POST", "/api/remember", {"text": "auth.py uses JWT #security"})
    assert status == 200 and body["id"].startswith("note:")
    status, graph = call(app, "GET", "/api/graph")
    ids = {n["id"] for n in graph["nodes"]}
    assert {"file:auth.py", "topic:security", body["id"]} <= ids
    assert all({"src", "dst", "rel"} <= set(e) for e in graph["edges"])
    status, ctx = call(app, "GET", "/api/context?q=jwt")
    assert status == 200 and "JWT" in ctx["text"] and ctx["nodes"][0]["id"] == body["id"]
    assert call(app, "POST", "/api/remember", {"text": "x"}, token=False)[0] == 403


# 47
def test_web_runs_feed_the_graph(app, tmp_path):
    call(app, "POST", "/api/run", {"instruction": "create notes.md with hi"})
    req, _ = wait_for(app, "approval_request")
    call(app, "POST", "/api/approve", {"id": req["id"], "decision": "y"})
    wait_for(app, "final")
    _, graph = call(app, "GET", "/api/graph")
    assert "file:notes.md" in {n["id"] for n in graph["nodes"]}
    assert any(n["type"] == "task" for n in graph["nodes"])
