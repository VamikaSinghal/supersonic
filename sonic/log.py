"""Session log: one JSONL file per REPL session. [Owner: step 8]

Location: <ws.root>/.sonic/sessions/<YYYYmmdd-HHMMSS>-<pid>.jsonl
Each line is a JSON object with at least {"ts": <iso8601>, "event": <kind>, ...}:
  {"event": "instruction", "text": ...}
  {"event": "step", "tool": ..., "args": {...}, "ok": bool, "observation": <truncated to 2000 chars>}
  {"event": "final", "message": ...}
"""
from pathlib import Path

from sonic.planner import Step
from sonic.workspace import Workspace

MAX_LOGGED_OBSERVATION = 2000


class SessionLog:
    def __init__(self, ws: Workspace):
        self.path: Path = ...

    def instruction(self, text: str) -> None:
        raise NotImplementedError

    def step(self, step: Step) -> None:
        raise NotImplementedError

    def final(self, message: str) -> None:
        raise NotImplementedError

    def recent(self, n: int = 10) -> str:
        """Human-readable last n step events for /log, e.g. '✓ write_file a.txt' lines; 'no steps yet' if none."""
        raise NotImplementedError
