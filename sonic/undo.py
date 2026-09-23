"""Undo for file changes made by the harness. [Owner: step 7]

Nothing is ever deleted: undoing a created file moves it into TRASH_DIR, and
undoing an edit moves the replaced version into TRASH_DIR before restoring.
"""
from dataclasses import dataclass

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

    def snapshot(self, action: Action) -> Change | None:
        """Capture the target file's state before a mutating action; None for other tools."""
        raise NotImplementedError

    def push(self, change: Change) -> None:
        raise NotImplementedError

    def __len__(self) -> int:
        raise NotImplementedError

    def undo(self) -> str:
        """Revert the most recent change. Returns a message; 'nothing to undo' if empty.

        The current file (if any) is moved to TRASH_DIR/<n>/<path>, never deleted,
        and the message says where it went.
        """
        raise NotImplementedError
