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


# 11
def test_then_only_splits_between_commands_outside_quotes():
    p = StubPlanner()
    assert p.next_action("run if true; then echo yes; fi", []) == Action(
        "run_shell", {"command": "if true; then echo yes; fi"}
    )
    instr = 'run echo "a then b" then read x.txt'
    a1 = p.next_action(instr, [])
    assert a1 == Action("run_shell", {"command": 'echo "a then b"'})
    assert p.next_action(instr, [Step(a1, True, "")]) == Action("read_file", {"path": "x.txt"})


# 12
def test_unquoted_paths_with_spaces():
    p = StubPlanner()
    assert p.next_action("read my notes.txt", []) == Action("read_file", {"path": "my notes.txt"})
    assert p.next_action("create my notes.txt with hello world", []) == Action(
        "write_file", {"path": "my notes.txt", "content": "hello world"}
    )
    assert p.next_action("replace a with b in my file.txt", []) == Action(
        "edit_file", {"path": "my file.txt", "old": "a", "new": "b"}
    )
    assert p.next_action("write hi to my file.txt", []) == Action(
        "write_file", {"path": "my file.txt", "content": "hi"}
    )
