"""Local web UI: `python -m sonic --web`. [Owner: UI agent]

Stdlib only (http.server + threading). Serves sonic/static/index.html and a small JSON API.
Binds to 127.0.0.1 only. Rejects requests whose Host header isn't localhost/127.0.0.1
(DNS-rebinding guard) with 403, and every POST must carry header X-Sonic-Token: <app.token>
(the page gets the token embedded in a <meta name="sonic-token"> tag) or gets 403.

API:
  GET  /                      -> the page
  GET  /api/state             -> {"workspace", "sandbox": bool, "busy": bool, "undo_count": int, "planner": str}
  GET  /api/events?after=N    -> {"events": [...], "next": M}   (events with seq > N; long-poll optional)
  POST /api/run      {"instruction"}                     -> 202 {"ok": true}; 409 if a run is in progress
  POST /api/approve  {"id", "decision": "y"|"n"|"a"}      -> 200; "a" approves everything for the rest of the app's life
  POST /api/undo                                          -> 200 {"message"}
Events (each has "seq" and "type"):
  {"type": "instruction", "text"}
  {"type": "approval_request", "id", "tool", "preview", "diff"}   # diff: unified diff for write/edit, "" otherwise
  {"type": "step", "tool", "args", "ok", "observation"}
  {"type": "final", "message"}
Reads (read_file, list_dir) never need approval. Uses Agent with UndoStack and SessionLog like the REPL.

Additive extras: /api/state also has "auto_approve"; events "approval_resolved" {"id", "decision"}
and "undo" {"message"} let every open tab (and a reload) replay the same timeline.
/api/undo answers 409 while a run is in progress.

Brain (the context graph, sonic/context.py; one shared instance, guarded by a lock):
  GET  /api/graph             -> graph.to_json(): {"nodes": [...], "edges": [...]}
  GET  /api/context?q=...     -> {"text": render_context(q), "nodes": relevant(q)}
  POST /api/remember {"text"} -> {"id", "linked": [neighbour ids]}   (saved)
  POST /api/forget   {"id"}   -> {"ok": true}; 404 if unknown          (archived, never erased; saved)
Each remember/forget also emits {"type": "graph_changed"}. If the graph can't load, these answer 503.
"""
import difflib
import hmac
import html
import json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from sonic.agent import Agent
from sonic.context import ContextGraph
from sonic.errors import ToolError
from sonic.log import SessionLog
from sonic.planner import Action, Planner, Step
from sonic.undo import UndoStack
from sonic.workspace import Workspace

SAFE_TOOLS = {"read_file", "list_dir"}
PAGE = Path(__file__).parent / "static" / "index.html"
MAX_BODY = 1 << 20
MAX_OBSERVATION = 20_000
MAX_DIFF = 200_000
APPROVAL_TIMEOUT = 600.0
LONG_POLL = 1.0
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def preview(action: Action) -> str:
    """One-line summary of an action, matching the REPL's approval prompt."""
    a = action.args
    if action.tool == "write_file":
        return f"write_file {a.get('path')} ({len(str(a.get('content', '')).encode())} bytes)"
    if action.tool == "edit_file":
        return f"edit_file {a.get('path')}: {a.get('old')!r} → {a.get('new')!r}"
    if action.tool == "run_shell":
        return f"run_shell: {a.get('command')}"
    return f"{action.tool} {a}"


def make_diff(ws: Workspace, action: Action) -> str:
    """Unified diff of what a write_file/edit_file would do; "" for anything else or on error."""
    a = action.args
    path = a.get("path")
    if action.tool not in ("write_file", "edit_file") or not isinstance(path, str):
        return ""
    try:
        target = ws.resolve(path)
        if target.is_dir():
            return ""
        current = target.read_text(errors="replace") if target.is_file() else ""
    except (ToolError, OSError):
        return ""
    if action.tool == "write_file":
        new = str(a.get("content", ""))
    else:
        old, repl = a.get("old"), a.get("new")
        if not isinstance(old, str) or not isinstance(repl, str):
            return ""
        new = current.replace(old, repl, 1)
    diff = "\n".join(difflib.unified_diff(
        current.splitlines(), new.splitlines(), f"a/{path}", f"b/{path}", lineterm=""))
    return diff if len(diff) <= MAX_DIFF else diff[:MAX_DIFF] + "\n… (diff truncated)"


def _planner_name(planner: Planner) -> str:
    name = type(planner).__name__
    model = getattr(planner, "model", None)
    return f"{name} · {model}" if isinstance(model, str) else name


class LockedGraph:
    """Thread-safe facade: every ContextGraph method call holds one shared lock."""

    def __init__(self, graph: ContextGraph):
        self._graph = graph
        self.lock = threading.RLock()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._graph, name)
        if not callable(attr):
            return attr

        def locked(*args, **kwargs):
            with self.lock:
                return attr(*args, **kwargs)
        return locked


def _open_graph(ws: Workspace, planner: Planner, graph: ContextGraph | None) -> LockedGraph | None:
    """Reuse `graph` or the one the planner already recalls from, else load one; None if it can't load.
    A planner recalling from the graph is rebound to the locked facade."""
    recall = getattr(planner, "context", None)
    if graph is None and isinstance(getattr(recall, "__self__", None), ContextGraph):
        graph = recall.__self__
    try:
        locked = LockedGraph(graph if graph is not None else ContextGraph(ws))
    except Exception as e:
        print(f"warning: context graph unavailable: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    if recall is not None and getattr(recall, "__self__", None) is locked._graph:
        planner.context = locked.render_context
    return locked


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    app: "WebApp"


class WebApp:
    token: str
    url: str   # e.g. "http://127.0.0.1:54321/"

    def __init__(self, ws: Workspace, planner: Planner, port: int, yolo: bool, max_steps: int,
                 context: ContextGraph | None = None):
        self.ws = ws
        self.planner_name = _planner_name(planner)
        self.token = secrets.token_urlsafe(32)
        self.undo = UndoStack(ws)
        self.log = SessionLog(ws)
        self.graph = _open_graph(ws, planner, context)
        self.agent = Agent(ws, planner, max_steps=max_steps, approve=self._approve,
                           on_step=self._on_step, undo=self.undo, context=self.graph)  # type: ignore[arg-type]
        self._always = yolo
        self._busy = False
        self._closed = False
        self._events: list[dict] = []
        self._cond = threading.Condition()
        self._pending: dict[str, dict] = {}   # id -> {"done": Event, "decision": str}
        self._server = _Server(("127.0.0.1", port), _Handler)
        self._server.app = self
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def serve_forever(self) -> None:
        self._server.serve_forever(poll_interval=0.1)

    def shutdown(self) -> None:
        with self._cond:
            self._closed = True
            for pending in self._pending.values():
                pending["done"].set()
            self._cond.notify_all()
        self._server.shutdown()
        self._server.server_close()

    # --- events ---

    def _emit(self, type_: str, **fields) -> None:
        with self._cond:
            self._emit_locked(type_, **fields)

    def _emit_locked(self, type_: str, **fields) -> None:
        self._events.append({"seq": len(self._events) + 1, "type": type_, **fields})
        self._cond.notify_all()

    def events_after(self, after: int, wait: float = LONG_POLL) -> tuple[list[dict], int]:
        """Events with seq > after, waiting up to `wait` seconds for the first one."""
        with self._cond:
            if len(self._events) <= after and not self._closed:
                self._cond.wait_for(lambda: len(self._events) > after or self._closed, timeout=wait)
            return self._events[max(after, 0):], len(self._events)

    def state(self) -> dict:
        with self._cond:
            busy = self._busy
        return {"workspace": str(self.ws.root), "sandbox": self.ws.sandboxed, "busy": busy,
                "undo_count": len(self.undo), "planner": self.planner_name,
                "auto_approve": self._always, "brain": self.graph is not None}

    # --- runs ---

    def start_run(self, instruction: str) -> bool:
        """Start a run in a background thread; False if one is already in progress."""
        with self._cond:
            if self._busy or self._closed:
                return False
            self._busy = True
            self._emit_locked("instruction", text=instruction)
        self.log.instruction(instruction)
        threading.Thread(target=self._run, args=(instruction,), daemon=True).start()
        return True

    def _run(self, instruction: str) -> None:
        try:
            _, message = self.agent.run(instruction)
        except Exception as e:  # e.g. a planner's network error
            message = f"error: {type(e).__name__}: {e}"
        self.log.final(message)
        with self._cond:
            self._busy = False
            self._emit_locked("final", message=message)

    def _on_step(self, step: Step) -> None:
        self.log.step(step)
        obs = step.observation
        if len(obs) > MAX_OBSERVATION:
            obs = f"{obs[:MAX_OBSERVATION]}…[truncated {len(obs) - MAX_OBSERVATION} chars]"
        self._emit("step", tool=step.action.tool, args=step.action.args, ok=step.ok, observation=obs)

    def _approve(self, action: Action) -> bool:
        """Runs on the worker thread: auto-approve reads, otherwise ask the page and block."""
        if self._always or action.tool in SAFE_TOOLS:
            return True
        id_ = secrets.token_hex(6)
        pending = {"done": threading.Event(), "decision": "n"}
        with self._cond:
            if self._closed:
                return False
            self._pending[id_] = pending
            self._emit_locked("approval_request", id=id_, tool=action.tool, args=action.args,
                              preview=preview(action), diff=make_diff(self.ws, action))
        answered = pending["done"].wait(APPROVAL_TIMEOUT)
        with self._cond:
            self._pending.pop(id_, None)
            if not answered:
                self._emit_locked("approval_resolved", id=id_, decision="timeout")
        return answered and pending["decision"] in ("y", "a")

    def answer(self, id_: str, decision: str) -> bool:
        """Deliver a y/n/a decision; False if no such pending approval."""
        with self._cond:
            pending = self._pending.pop(id_, None)
            if pending is None:
                return False
            if decision == "a":
                self._always = True
            pending["decision"] = decision
            self._emit_locked("approval_resolved", id=id_, decision=decision)
        pending["done"].set()
        return True

    def do_undo(self) -> str | None:
        """Undo the last change; None while a run is in progress."""
        with self._cond:
            if self._busy:
                return None
            message = self.undo.undo()
            self._emit_locked("undo", message=message)
        return message

    # --- brain ---

    def graph_json(self) -> dict:
        assert self.graph is not None
        return self.graph.to_json()

    def context_for(self, query: str) -> dict:
        assert self.graph is not None
        if not query.strip():
            return {"text": "", "nodes": []}
        with self.graph.lock:
            return {"text": self.graph.render_context(query), "nodes": self.graph.relevant(query)}

    def remember(self, text: str) -> dict:
        assert self.graph is not None
        with self.graph.lock:
            id_ = self.graph.remember(text)
            linked = [n["id"] for n in self.graph.neighbors(id_)]
            self.graph.save()
        self._emit("graph_changed")
        return {"id": id_, "linked": linked}

    def forget(self, id_: str) -> bool:
        assert self.graph is not None
        with self.graph.lock:
            if not self.graph.forget(id_):
                return False
            self.graph.save()
        self._emit("graph_changed")
        return True

    def page(self) -> bytes:
        text = PAGE.read_text(encoding="utf-8")
        text = text.replace("{{TOKEN}}", html.escape(self.token))
        text = text.replace("{{WORKSPACE}}", html.escape(str(self.ws.root)))
        return text.encode()


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "sonic"
    sys_version = ""

    def log_message(self, format: str, *args) -> None:
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data, default=str).encode(), "application/json")

    def _host_ok(self) -> bool:
        if self.headers.get("Host", "") in self.server.app.hosts:
            return True
        self._json(403, {"error": "forbidden host"})
        return False

    def do_GET(self) -> None:
        if not self._host_ok():
            return
        app = self.server.app
        url = urlsplit(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, app.page(), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            self._json(200, app.state())
        elif url.path == "/api/events":
            try:
                after = int(parse_qs(url.query).get("after", ["0"])[0])
            except ValueError:
                return self._json(400, {"error": "bad 'after'"})
            events, next_ = app.events_after(after)
            self._json(200, {"events": events, "next": next_})
        elif url.path == "/api/graph":
            if self._brain_ok():
                self._json(200, app.graph_json())
        elif url.path == "/api/context":
            if self._brain_ok():
                self._json(200, app.context_for(parse_qs(url.query).get("q", [""])[0]))
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._host_ok():
            return
        app = self.server.app
        if not hmac.compare_digest(self.headers.get("X-Sonic-Token", ""), app.token):
            return self._json(403, {"error": "missing or bad token"})
        body = self._body()
        if body is None:
            return
        path = urlsplit(self.path).path
        if path == "/api/run":
            instruction = body.get("instruction")
            if not isinstance(instruction, str) or not instruction.strip():
                return self._json(400, {"error": "instruction required"})
            if not app.start_run(instruction.strip()):
                return self._json(409, {"error": "a run is already in progress"})
            self._json(202, {"ok": True})
        elif path == "/api/approve":
            id_, decision = body.get("id"), body.get("decision")
            if not isinstance(id_, str) or decision not in ("y", "n", "a"):
                return self._json(400, {"error": "need id and decision y|n|a"})
            if not app.answer(id_, decision):
                return self._json(404, {"error": "no such pending approval"})
            self._json(200, {"ok": True})
        elif path == "/api/undo":
            message = app.do_undo()
            if message is None:
                return self._json(409, {"error": "a run is in progress"})
            self._json(200, {"message": message})
        elif path == "/api/remember":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                return self._json(400, {"error": "text required"})
            if self._brain_ok():
                self._json(200, app.remember(text.strip()))
        elif path == "/api/forget":
            id_ = body.get("id")
            if not isinstance(id_, str) or not id_:
                return self._json(400, {"error": "id required"})
            if not self._brain_ok():
                return
            if not app.forget(id_):
                return self._json(404, {"error": "no such node"})
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def _brain_ok(self) -> bool:
        if self.server.app.graph is not None:
            return True
        self._json(503, {"error": "context graph unavailable"})
        return False

    def _body(self) -> dict | None:
        """Parse a JSON object body (empty = {}); sends an error and returns None if invalid."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._json(413, {"error": "body too large"})
            return None
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw) if raw.strip() else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            self._json(400, {"error": "body must be a JSON object"})
            return None
        return data


def create_app(ws: Workspace, planner: Planner, port: int = 0, yolo: bool = False,
               max_steps: int = 10, context: ContextGraph | None = None) -> WebApp:
    """Bind the server (port 0 = pick a free port) without starting it.
    context: the graph to share; default = the planner's recall graph, else a fresh load of ws's."""
    return WebApp(ws, planner, port, yolo, max_steps, context)
