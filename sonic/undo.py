"""Undo for file changes made by the harness. [Owner: step 7]

Nothing is ever deleted: undoing a created file moves it into TRASH_DIR, and
undoing an edit moves the replaced version into TRASH_DIR before restoring.
If the file was changed outside the harness after the recorded change, that
external version is what goes to the trash, and the message says so.
When an undo cannot be done safely (the path is now a directory, a parent is
now a file, or it resolves outside the workspace) nothing is touched and the
change stays on the stack.
"""
import os
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
    after: bytes | None = None  # content right after the change (set by push); None = unknown


def _read_file(p: Path) -> bytes | None:
    """Bytes of a regular file, or None if it isn't one / can't be read."""
    try:
        return p.read_bytes() if p.is_file() else None
    except (OSError, ValueError):
        return None


class UndoStack:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self._changes: list[Change] = []
        self._next_slot = 1

    def snapshot(self, action: Action) -> Change | None:
        """Capture the target file's state before a mutating action; None for other tools
        or when the path is unusable (the tool itself will report that error)."""
        path = action.args.get("path")
        if action.tool not in MUTATING_TOOLS or not isinstance(path, str):
            return None
        try:
            target = self.ws.resolve(path)
            if target.is_dir() or self.ws.is_reserved(target):
                return None
            before = target.read_bytes() if target.is_file() else None
        except (ToolError, OSError, ValueError):
            return None
        return Change(action.tool, path, before)

    def push(self, change: Change) -> None:
        """Record a change after its tool succeeded (remembers the resulting content)."""
        if change.after is None:
            try:
                change.after = _read_file(self.ws.resolve(change.path))
            except ToolError:
                pass
        self._changes.append(change)

    def __len__(self) -> int:
        return len(self._changes)

    def undo(self) -> str:
        """Revert the most recent change. Returns a message; 'nothing to undo' if empty.

        The current file (if any) is moved to TRASH_DIR/<n>/<path>, never deleted,
        and the message says where it went. On refusal ('cannot undo ...') nothing
        changes on disk and the change stays on the stack.
        """
        if not self._changes:
            return "nothing to undo"
        change = self._changes[-1]
        refuse = f"cannot undo {change.tool} {change.path}"
        try:
            target = self.ws.resolve(change.path)
        except ToolError as e:
            return f"{refuse}: {e}"
        if target.is_dir():
            return f"{refuse}: it is now a directory; move it aside and try again"
        if change.before is not None:
            for parent in target.parents:
                if parent == self.ws.root:
                    break
                if os.path.lexists(parent) and not parent.is_dir():
                    rel = parent.relative_to(self.ws.root).as_posix()
                    return f"{refuse}: {rel} is now a file, not a directory"

        note = ""
        try:
            if os.path.lexists(target):
                current = _read_file(target)
                modified = change.after is not None and current != change.after
                rel = target.relative_to(self.ws.root)
                dest = self._new_slot() / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                target.replace(dest)
                where = dest.relative_to(self.ws.root).as_posix()
                if modified:
                    note = f" (file was modified outside the harness since; that version was moved to {where})"
                else:
                    kind = "created file" if change.before is None else "replaced version"
                    note = f" ({kind} moved to {where})"
            elif change.before is not None:
                note = " (file had been removed; restored)"
            if change.before is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(change.before)
        except OSError as e:
            return f"{refuse}: {e.strerror or e}{note}"
        self._changes.pop()
        return f"undid {change.tool} {change.path}{note}"

    def _new_slot(self) -> Path:
        """A fresh, newly created numbered trash dir, skipping slots left by earlier sessions."""
        trash = self.ws.root / TRASH_DIR
        if trash.is_dir():
            taken = [int(p.name) for p in trash.iterdir() if p.name.isdigit()]
            self._next_slot = max([self._next_slot, *(n + 1 for n in taken)])
        while True:
            slot = trash / str(self._next_slot)
            self._next_slot += 1
            try:
                slot.mkdir(parents=True)
                return slot
            except FileExistsError:
                continue  # another stack took it between the scan and now
