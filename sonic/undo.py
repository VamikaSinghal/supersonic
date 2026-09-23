"""Undo for file changes made by the harness. [Owner: step 7]

Nothing is ever deleted: undoing a created file moves it into TRASH_DIR, and
undoing an edit moves the replaced version into TRASH_DIR before restoring.
"""
from dataclasses import dataclass
from pathlib import Path

from sonic.errors import ToolError
from sonic.planner import Action
from sonic.workspace import Workspace

TRASH_DIR = ".sonic/trash"
MUTATING_TOOLS = {"write_file", "edit_file"}


@dataclass
class Change:
    tool: str
    path: str              # workspace-relative, as given in the action
    before: bytes | None   # None = the file did not exist


class UndoStack:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self._changes: list[Change] = []
        self._next_slot = 1

    def snapshot(self, action: Action) -> Change | None:
        """Capture the target file's state before a mutating action; None for other tools."""
        path = action.args.get("path")
        if action.tool not in MUTATING_TOOLS or not isinstance(path, str):
            return None
        try:
            target = self.ws.resolve(path)
        except ToolError:
            return None  # the tool itself will report the error
        if target.is_dir():
            return None
        before = target.read_bytes() if target.is_file() else None
        return Change(action.tool, path, before)

    def push(self, change: Change) -> None:
        self._changes.append(change)

    def __len__(self) -> int:
        return len(self._changes)

    def undo(self) -> str:
        """Revert the most recent change. Returns a message; 'nothing to undo' if empty.

        The current file (if any) is moved to TRASH_DIR/<n>/<path>, never deleted,
        and the message says where it went.
        """
        if not self._changes:
            return "nothing to undo"
        change = self._changes[-1]
        try:
            target = self.ws.resolve(change.path)
        except ToolError as e:
            return f"cannot undo {change.tool} {change.path}: {e}"
        self._changes.pop()
        note = ""
        if target.exists():
            rel = target.relative_to(self.ws.root)
            dest = self._new_slot() / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            target.replace(dest)
            kind = "created file" if change.before is None else "replaced version"
            note = f" ({kind} moved to {dest.relative_to(self.ws.root).as_posix()})"
        if change.before is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(change.before)
        return f"undid {change.tool} {change.path}{note}"

    def _new_slot(self) -> Path:
        """A fresh numbered trash dir, skipping slots left by earlier sessions."""
        trash = self.ws.root / TRASH_DIR
        if trash.is_dir():
            taken = [int(p.name) for p in trash.iterdir() if p.name.isdigit()]
            self._next_slot = max([self._next_slot, *(n + 1 for n in taken)])
        slot = trash / str(self._next_slot)
        self._next_slot += 1
        return slot
