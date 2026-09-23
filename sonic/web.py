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
"""
from sonic.planner import Planner
from sonic.workspace import Workspace


class WebApp:
    token: str
    url: str   # e.g. "http://127.0.0.1:54321/"

    def serve_forever(self) -> None:
        raise NotImplementedError

    def shutdown(self) -> None:
        raise NotImplementedError


def create_app(ws: Workspace, planner: Planner, port: int = 0, yolo: bool = False) -> WebApp:
    """Bind the server (port 0 = pick a free port) without starting it."""
    raise NotImplementedError
