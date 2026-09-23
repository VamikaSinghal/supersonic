"""Commit 5: agent loop."""
from sonic.agent import Agent
from sonic.planner import Action, Done, StubPlanner
from sonic.workspace import Workspace


class ScriptedPlanner:
    def __init__(self, actions):
        self.actions = actions

    def next_action(self, instruction, history):
        return self.actions[len(history)] if len(history) < len(self.actions) else Done("done")


class ForeverPlanner:
    def next_action(self, instruction, history):
        return Action("list_dir", {"path": "."})


# 9
def test_agent_runs_stub_plan_against_real_filesystem(tmp_path):
    agent = Agent(Workspace(tmp_path), StubPlanner())
    steps, final = agent.run("create hello.py with print('hi') then run python3 hello.py")
    assert (tmp_path / "hello.py").read_text() == "print('hi')"
    assert [s.ok for s in steps] == [True, True]
    assert "hi" in steps[1].observation


# 10
def test_agent_survives_errors_denials_and_caps_steps(tmp_path):
    ws = Workspace(tmp_path)
    denied = Agent(
        ws,
        ScriptedPlanner([
            Action("read_file", {"path": "missing.txt"}),   # ToolError
            Action("nope_tool", {}),                        # unknown tool
            Action("read_file", {"wrong_arg": 1}),          # bad args
            Action("write_file", {"path": "x", "content": "y"}),  # denied
        ]),
        approve=lambda a: a.tool != "write_file",
    )
    steps, _ = denied.run("anything")
    assert [s.ok for s in steps] == [False, False, False, False]
    assert "denied" in steps[3].observation.lower()
    assert not (tmp_path / "x").exists()

    seen = []
    capped = Agent(ws, ForeverPlanner(), max_steps=3, on_step=seen.append)
    steps, final = capped.run("loop")
    assert len(steps) == 3 and len(seen) == 3
    assert "max" in final.lower()
