"""Commit 4: stub planner."""
from sonic.planner import Action, Done, StubPlanner, Step


# 8
def test_stub_planner_emits_chained_actions_then_done():
    p = StubPlanner()
    instr = "create hello.py with print('hi') then run python hello.py"
    history: list[Step] = []

    a1 = p.next_action(instr, history)
    assert a1 == Action("write_file", {"path": "hello.py", "content": "print('hi')"})
    history.append(Step(a1, True, "wrote"))

    a2 = p.next_action(instr, history)
    assert a2 == Action("run_shell", {"command": "python hello.py"})
    history.append(Step(a2, True, "hi"))

    assert isinstance(p.next_action(instr, history), Done)

    assert p.next_action("replace hi with hello in hello.py", []) == Action(
        "edit_file", {"path": "hello.py", "old": "hi", "new": "hello"}
    )
    help_ = p.next_action("make me a sandwich", [])
    assert isinstance(help_, Done) and "run" in help_.message.lower()
