"""Session log: one JSONL file per REPL session. [Owner: step 8]

Location: <ws.root>/.sonic/sessions/<YYYYmmdd-HHMMSS>-<pid>.jsonl
Each line is a JSON object with at least {"ts": <iso8601>, "event": <kind>, ...}:
  {"event": "instruction", "text": ...}
  {"event": "step", "tool": ..., "args": {...}, "ok": bool, "observation": <truncated to 2000 chars>}
  {"event": "final", "message": ...}
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from sonic.planner import Step
from sonic.workspace import Workspace

MAX_LOGGED_OBSERVATION = 2000
_SUMMARY_WIDTH = 100


def _truncate(text: str, limit: int = MAX_LOGGED_OBSERVATION) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…[truncated {len(text) - limit} chars]"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class SessionLog:
    """Append-only JSONL log of one REPL session; never raises on I/O errors."""

    def __init__(self, ws: Workspace):
        self._warned = False
        directory = ws.root / ".sonic" / "sessions"
        stem = f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}"
        self.path: Path = directory / f"{stem}.jsonl"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            counter = 1
            while self.path.exists():
                self.path = directory / f"{stem}-{counter}.jsonl"
                counter += 1
            self.path.touch()
        except OSError as e:
            self._warn(e)

    def instruction(self, text: str) -> None:
        self._write("instruction", text=text)

    def step(self, step: Step) -> None:
        raw = step.action.args if isinstance(step.action.args, dict) else {}
        args = {str(k): _truncate(v) if isinstance(v, str) else v for k, v in raw.items()}
        self._write("step", tool=step.action.tool, args=args, ok=step.ok,
                    observation=_truncate(str(step.observation)))

    def final(self, message: str) -> None:
        self._write("final", message=message)

    def recent(self, n: int = 10) -> str:
        """Human-readable last n step events for /log, e.g. '✓ write_file a.txt' lines; 'no steps yet' if none."""
        steps = []
        try:
            with self.path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and event.get("event") == "step":
                        steps.append(event)
        except OSError:
            pass
        if not steps or n <= 0:
            return "no steps yet"
        return "\n".join(_summarize(e) for e in steps[-n:])

    def _write(self, event: str, **fields) -> None:
        record = {"ts": _now(), "event": event, **fields}
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except (OSError, TypeError, ValueError) as e:
            self._warn(e)

    def _warn(self, error: Exception) -> None:
        if not self._warned:
            self._warned = True
            print(f"warning: session log disabled ({error})", file=sys.stderr)


def _summarize(event: dict) -> str:
    """One line for a logged step; tolerates hand-edited or foreign records."""
    args = event.get("args")
    if not isinstance(args, dict):
        args = {}
    tool = event.get("tool") or "?"
    if tool == "run_shell":
        target = f": {args.get('command', '')}"
    elif "path" in args:
        target = f" {args['path']}"
    else:
        target = ""
    observation = event.get("observation")
    obs = (observation if isinstance(observation, str) else "").strip().splitlines()
    first = obs[0] if obs else ""
    line = f"{'✓' if event.get('ok') else '✗'} {tool}{target}"
    if first:
        line += f" → {first}"
    line = " ".join(line.split("\n"))
    return line if len(line) <= _SUMMARY_WIDTH else line[: _SUMMARY_WIDTH - 1] + "…"
