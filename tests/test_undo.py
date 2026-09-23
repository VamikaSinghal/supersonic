"""Step 7: undo that never deletes."""
from sonic.agent import Agent
from sonic.planner import Action, Done
from sonic.tools import fs
from sonic.undo import TRASH_DIR, UndoStack
from sonic.workspace import Workspace


class ScriptedPlanner:
    def __init__(self, actions):
        self.actions = actions

    def next_action(self, instruction, history):
        return self.actions[len(history)] if len(history) < len(self.actions) else Done("done")


def trashed(root):
    return sorted(p.read_text() for p in (root / TRASH_DIR).rglob("*") if p.is_file())


def apply(stack, ws, action):
    change = stack.snapshot(action)
    getattr(fs, action.tool)(ws, **action.args)
    stack.push(change)


# 23
def test_undo_edit_restores_previous_and_keeps_replaced_version(tmp_path):
    ws = Workspace(tmp_path)
    (tmp_path / "a.txt").write_text("v1")
    stack = UndoStack(ws)
    apply(stack, ws, Action("edit_file", {"path": "a.txt", "old": "v1", "new": "v2"}))
    assert (tmp_path / "a.txt").read_text() == "v2"
    msg = stack.undo()
    assert (tmp_path / "a.txt").read_text() == "v1"
    assert "a.txt" in msg and TRASH_DIR in msg
    assert trashed(tmp_path) == ["v2"]


# 24
def test_undo_create_moves_file_to_trash_lifo_and_empty(tmp_path):
    ws = Workspace(tmp_path)
    stack = UndoStack(ws)
    assert "nothing to undo" in stack.undo().lower()
    apply(stack, ws, Action("write_file", {"path": "n/new.txt", "content": "first"}))
    apply(stack, ws, Action("write_file", {"path": "n/new.txt", "content": "second"}))
    assert len(stack) == 2
    stack.undo()
    assert (tmp_path / "n/new.txt").read_text() == "first"
    stack.undo()
    assert not (tmp_path / "n/new.txt").exists()
    assert trashed(tmp_path) == ["first", "second"]
    assert stack.snapshot(Action("read_file", {"path": "x"})) is None


# 25
def test_agent_records_only_successful_mutations(tmp_path):
    ws = Workspace(tmp_path)
    (tmp_path / "a.txt").write_text("orig")
    stack = UndoStack(ws)
    agent = Agent(ws, ScriptedPlanner([
        Action("edit_file", {"path": "a.txt", "old": "orig", "new": "one"}),
        Action("edit_file", {"path": "a.txt", "old": "zzz", "new": "x"}),   # fails: not found
        Action("write_file", {"path": "b.txt", "content": "b"}),            # denied
        Action("read_file", {"path": "a.txt"}),                             # not mutating
    ]), approve=lambda a: a.tool != "write_file", undo=stack)
    agent.run("go")
    assert len(stack) == 1
    stack.undo()
    assert (tmp_path / "a.txt").read_text() == "orig"
