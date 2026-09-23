"""Edge cases for the agent loop, StubPlanner and LLMPlanner (no network: clients are faked)."""
import time

import pytest

import sonic.agent as agent_mod
from sonic.agent import Agent
from sonic.llm_planner import LLMPlanner
from sonic.planner import Action, Done, Step, StubPlanner, HELP
from sonic.workspace import Workspace


# ---------------------------------------------------------------- helpers

class RaisingPlanner:
    def __init__(self, exc):
        self.exc = exc

    def next_action(self, instruction, history):
        raise self.exc


class SeqPlanner:
    """Returns the given decisions in order, then Done('done')."""
    def __init__(self, *decisions):
        self.decisions = decisions

    def next_action(self, instruction, history):
        return self.decisions[len(history)] if len(history) < len(self.decisions) else Done("done")


class FakeGraph:
    def __init__(self):
        self.saved = 0
        self.steps = []

    def start_task(self, instruction):
        return "task:1"

    def record_step(self, task, step):
        self.steps.append(step)

    def save(self):
        self.saved += 1


def reply(*blocks, stop_reason="tool_use"):
    return {"content": list(blocks), "stop_reason": stop_reason}


def text(t):
    return {"type": "text", "text": t}


# ---------------------------------------------------------------- agent: planner failures

@pytest.mark.parametrize("exc", [RuntimeError("Anthropic API request failed: timed out"),
                                 ValueError("boom"), KeyError("x")])
def test_planner_exception_becomes_final_message_and_graph_is_saved(tmp_path, exc):
    g = FakeGraph()
    steps, final = Agent(Workspace(tmp_path), RaisingPlanner(exc), context=g).run("go")
    assert steps == []
    assert final.startswith("planner error: ")
    assert type(exc).__name__ in final or str(exc) in final
    assert g.saved == 1


def test_planner_error_after_some_steps_keeps_the_steps(tmp_path):
    class Flaky:
        def next_action(self, instruction, history):
            if history:
                raise RuntimeError("network down")
            return Action("list_dir", {"path": "."})
    steps, final = Agent(Workspace(tmp_path), Flaky()).run("go")
    assert len(steps) == 1 and steps[0].ok
    assert final.startswith("planner error:") and "network down" in final


def test_keyboard_interrupt_from_planner_propagates_and_graph_is_saved(tmp_path):
    g = FakeGraph()
    with pytest.raises(KeyboardInterrupt):
        Agent(Workspace(tmp_path), RaisingPlanner(KeyboardInterrupt()), context=g).run("go")
    assert g.saved == 1


@pytest.mark.parametrize("garbage", [None, "read a.txt", 42, ["read_file"], {"tool": "read_file"}])
def test_planner_returning_garbage_never_crashes(tmp_path, garbage):
    steps, final = Agent(Workspace(tmp_path), SeqPlanner(garbage), max_steps=5).run("go")
    assert isinstance(final, str) and final
    assert all(isinstance(s, Step) and s.ok is False for s in steps)
    assert len(steps) <= 5


@pytest.mark.parametrize("args", ["path=a.txt", ["a.txt"], None, 7])
def test_action_with_non_dict_args_is_a_failed_step(tmp_path, args):
    (tmp_path / "a.txt").write_text("hi")
    steps, final = Agent(Workspace(tmp_path), SeqPlanner(Action("read_file", args))).run("go")
    assert len(steps) == 1 and steps[0].ok is False
    assert "argument" in steps[0].observation.lower()
    assert final == "done"


@pytest.mark.parametrize("tool", [None, 3, ["read_file"]])
def test_action_with_non_string_tool_is_a_failed_step(tmp_path, tool):
    steps, _ = Agent(Workspace(tmp_path), SeqPlanner(Action(tool, {}))).run("go")
    assert len(steps) == 1 and steps[0].ok is False
    assert "unknown tool" in steps[0].observation


def test_done_with_non_string_message_is_stringified(tmp_path):
    _, final = Agent(Workspace(tmp_path), SeqPlanner(Done(None))).run("go")
    assert isinstance(final, str)


# ---------------------------------------------------------------- agent: callbacks

def test_on_step_raising_does_not_stop_the_run(tmp_path, capsys):
    def bad(step):
        raise RuntimeError("ui exploded")
    planner = SeqPlanner(Action("list_dir", {"path": "."}), Action("list_dir", {"path": "."}))
    steps, final = Agent(Workspace(tmp_path), planner, on_step=bad).run("go")
    assert len(steps) == 2 and final == "done"
    assert "ui exploded" in capsys.readouterr().err


def test_on_step_keyboard_interrupt_propagates(tmp_path):
    def stop(step):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        Agent(Workspace(tmp_path), SeqPlanner(Action("list_dir", {})), on_step=stop).run("go")


def test_approve_raising_is_treated_as_denied(tmp_path):
    def bad(action):
        raise RuntimeError("tty closed")
    planner = SeqPlanner(Action("write_file", {"path": "x.txt", "content": "y"}))
    steps, _ = Agent(Workspace(tmp_path), planner, approve=bad).run("go")
    assert steps[0].ok is False and "denied" in steps[0].observation
    assert not (tmp_path / "x.txt").exists()


# ---------------------------------------------------------------- agent: max_steps

def test_max_steps_zero_returns_immediately(tmp_path):
    calls = []

    class Counting:
        def next_action(self, instruction, history):
            calls.append(1)
            return Action("list_dir", {})
    steps, final = Agent(Workspace(tmp_path), Counting(), max_steps=0).run("go")
    assert steps == [] and calls == []
    assert "max steps (0)" in final


@pytest.mark.parametrize("n", [-1, -100])
def test_negative_max_steps_is_rejected(tmp_path, n):
    with pytest.raises(ValueError):
        Agent(Workspace(tmp_path), StubPlanner(), max_steps=n)


# ---------------------------------------------------------------- agent: tool results

@pytest.mark.parametrize("value", [None, 42, b"bytes", ["a", "b"], {"k": 1}])
def test_tool_returning_non_string_gives_string_observation(tmp_path, monkeypatch, value):
    monkeypatch.setitem(agent_mod.TOOLS, "odd_tool", lambda ws: value)
    steps, _ = Agent(Workspace(tmp_path), SeqPlanner(Action("odd_tool", {}))).run("go")
    assert steps[0].ok is True and isinstance(steps[0].observation, str)


# ---------------------------------------------------------------- StubPlanner

def plan(instruction):
    """All actions StubPlanner would produce, or the final Done if it gives none."""
    p, history = StubPlanner(), []
    while True:
        d = p.next_action(instruction, history)
        if isinstance(d, Done):
            return history and [s.action for s in history] or d
        history.append(Step(d, True, "ok"))


@pytest.mark.parametrize("instruction", ["", "   ", "then", "  then  ", "and then", "run", "run   "])
def test_stub_empty_or_meaningless_instructions_give_help(instruction):
    assert plan(instruction) == Done(HELP)


@pytest.mark.parametrize("instruction", ["read a.txt then", "then read a.txt", "read a.txt and then",
                                         "  then read a.txt then  "])
def test_stub_dangling_then_is_ignored(instruction):
    assert plan(instruction) == [Action("read_file", {"path": "a.txt"})]


def test_stub_verbs_are_case_insensitive_and_paths_keep_case():
    assert plan("READ A.TXT") == [Action("read_file", {"path": "A.TXT"})]
    assert plan("Create Foo/Bar.PY with X THEN Run Echo HI") == [
        Action("write_file", {"path": "Foo/Bar.PY", "content": "X"}),
        Action("run_shell", {"command": "Echo HI"}),
    ]


@pytest.mark.parametrize("instruction", ["create x.txt with", "create x.txt with   ", 'create x.txt with ""'])
def test_stub_create_with_empty_content_is_an_empty_write(instruction):
    assert plan(instruction) == [Action("write_file", {"path": "x.txt", "content": ""})]


def test_stub_unicode_paths_and_content():
    assert plan("create données/日本.txt with héllo 🌍 then read données/日本.txt") == [
        Action("write_file", {"path": "données/日本.txt", "content": "héllo 🌍"}),
        Action("read_file", {"path": "données/日本.txt"}),
    ]


def test_stub_quoted_then_stays_literal():
    assert plan('create a.txt with "x then read b.txt" then read a.txt') == [
        Action("write_file", {"path": "a.txt", "content": "x then read b.txt"}),
        Action("read_file", {"path": "a.txt"}),
    ]


@pytest.mark.parametrize("instruction", [
    "create big.txt with " + "a" * 100_000,
    "create big.txt with " + "word then " * 10_000,
    "read a then " * 8_000 + "read b",
    "replace " + "with " * 20_000,
    "replace a with " + "b with " * 15_000,
    "replace a with " + "in " * 30_000 + "'",
    "write " + "with to " * 12_000,
    '"' * 100_000,
], ids=lambda s: repr(s[:24]))
def test_stub_very_long_instruction_parses_fast(instruction):
    t = time.perf_counter()
    StubPlanner().next_action(instruction, [])
    assert time.perf_counter() - t < 0.1


def test_stub_replace_still_parses_after_rewrite():
    assert plan("replace a with b in f.txt") == [Action("edit_file", {"path": "f.txt", "old": "a", "new": "b"})]
    assert plan("replace x with y in z in f.txt") == [
        Action("edit_file", {"path": "f.txt", "old": "x", "new": "y in z"})]
    assert plan("replace x with y in 'my in file.txt'") == [
        Action("edit_file", {"path": "my in file.txt", "old": "x", "new": "y"})]
    assert plan("REPLACE a with b with c IN f.txt") == [
        Action("edit_file", {"path": "f.txt", "old": "a", "new": "b with c"})]
    assert plan("replace a with b") == Done(HELP)
    assert plan("replace a with b in 'f.txt") == Done(HELP)


def test_stub_is_deterministic_and_done_past_the_plan():
    p = StubPlanner()
    instr = "read a then list then run echo hi"
    first = [p.next_action(instr, [Step(Action("x"), True, "")] * n) for n in range(3)]
    again = [p.next_action(instr, [Step(Action("x"), True, "")] * n) for n in range(3)]
    assert first == again and all(isinstance(a, Action) for a in first)
    for n in (3, 4, 50):
        d = p.next_action(instr, [Step(Action("x"), True, "")] * n)
        assert isinstance(d, Done) and f"completed {n} step(s)" in d.message


# ---------------------------------------------------------------- LLMPlanner

def planner_for(resp, **kw):
    return LLMPlanner(client=lambda body: resp, **kw)


@pytest.mark.parametrize("resp", [{}, {"stop_reason": "end_turn"}, {"content": None}, {"content": []}])
def test_llm_response_without_content_is_done(resp):
    d = planner_for(resp).next_action("go", [])
    assert isinstance(d, Done) and d.message


@pytest.mark.parametrize("content", ["just a string", 42, {"type": "text", "text": "hi"}, [None, "x", 3]])
def test_llm_content_not_a_list_of_blocks_is_done(content):
    d = planner_for({"content": content, "stop_reason": "end_turn"}).next_action("go", [])
    assert isinstance(d, Done) and isinstance(d.message, str)


def test_llm_non_dict_response_raises_runtime_error():
    with pytest.raises(RuntimeError):
        planner_for("not json").next_action("go", [])


@pytest.mark.parametrize("block", [
    {"type": "tool_use", "id": "t", "name": "list_dir"},
    {"type": "tool_use", "id": "t", "name": "list_dir", "input": None},
    {"type": "tool_use", "id": "t", "name": "list_dir", "input": "garbage"},
    {"type": "tool_use", "id": "t", "name": "list_dir", "input": ["a"]},
])
def test_llm_tool_use_with_missing_or_bad_input_gives_empty_args(block):
    assert planner_for(reply(block)).next_action("go", []) == Action("list_dir", {})


def test_llm_unknown_tool_passes_through_and_agent_reports_it(tmp_path):
    p = planner_for(reply({"type": "tool_use", "id": "t", "name": "launch_rockets", "input": {}}))
    assert p.next_action("go", []) == Action("launch_rockets", {})
    steps, _ = Agent(Workspace(tmp_path), p, max_steps=1).run("go")
    assert steps[0].ok is False and "unknown tool: launch_rockets" in steps[0].observation


def test_llm_tool_use_without_name_does_not_crash(tmp_path):
    p = planner_for(reply({"type": "tool_use", "id": "t", "input": {}}))
    steps, _ = Agent(Workspace(tmp_path), p, max_steps=1).run("go")
    assert steps[0].ok is False and "unknown tool" in steps[0].observation


def test_llm_max_tokens_text_only_is_done_with_cutoff_note():
    d = planner_for(reply(text("Half an answ"), stop_reason="max_tokens")).next_action("go", [])
    assert isinstance(d, Done)
    assert d.message.startswith("Half an answ") and "cut off" in d.message


def test_llm_client_exception_becomes_runtime_error():
    def client(body):
        raise ConnectionResetError("peer reset")
    with pytest.raises(RuntimeError, match="peer reset"):
        LLMPlanner(client=client).next_action("go", [])


def test_llm_client_runtime_error_propagates_and_agent_converts_it(tmp_path):
    def client(body):
        raise RuntimeError("Anthropic API error 529: overloaded")
    p = LLMPlanner(client=client)
    with pytest.raises(RuntimeError, match="529"):
        p.next_action("go", [])
    steps, final = Agent(Workspace(tmp_path), p).run("go")
    assert steps == [] and final.startswith("planner error:") and "529" in final


def test_llm_client_keyboard_interrupt_propagates():
    def client(body):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        LLMPlanner(client=client).next_action("go", [])


def test_llm_context_raising_still_builds_request(capsys):
    def ctx(q):
        raise OSError("graph file corrupt")
    body = LLMPlanner(client=lambda b: {}, context=ctx).build_request("go", [])
    assert body["messages"][0]["content"] == "go"
    assert "second brain" not in body["system"]
    assert "graph file corrupt" in capsys.readouterr().err


def test_llm_long_observations_are_truncated_in_request():
    huge = "x" * 500_000
    history = [Step(Action("read_file", {"path": "big"}), True, huge),
               Step(Action("read_file", {"path": "small"}), True, "short")]
    body = LLMPlanner(client=lambda b: {}).build_request("go", history)
    results = [m["content"][0]["content"] for m in body["messages"] if m["role"] == "user" and m is not body["messages"][0]]
    assert len(results[0]) <= 21_000 and "truncated" in results[0]
    assert results[0].startswith("x" * 1000)
    assert results[1] == "short"


def test_llm_history_with_non_dict_args_builds_valid_request():
    history = [Step(Action("read_file", "oops"), False, "error: bad arguments")]
    body = LLMPlanner(client=lambda b: {}).build_request("go", history)
    assert body["messages"][1]["content"][0]["input"] == {}


def test_llm_from_env_uses_sonic_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("SONIC_MODEL", "claude-test-model")
    assert LLMPlanner.from_env().model == "claude-test-model"


@pytest.mark.parametrize("key", ["", "   "])
def test_llm_from_env_rejects_empty_key(monkeypatch, key):
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        LLMPlanner.from_env()
