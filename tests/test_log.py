"""Step 8: JSONL session log."""
import json

from sonic.log import MAX_LOGGED_OBSERVATION, SessionLog
from sonic.planner import Action, Step
from sonic.workspace import Workspace


# 26
def test_session_log_writes_jsonl_events(tmp_path):
    log = SessionLog(Workspace(tmp_path))
    assert log.path.parent == tmp_path / ".sonic" / "sessions"
    log.instruction("create a.txt with hi")
    log.step(Step(Action("write_file", {"path": "a.txt", "content": "hi"}), True, "wrote 2 bytes"))
    log.step(Step(Action("run_shell", {"command": "yes"}), False, "x" * 10_000))
    log.final("completed 2 step(s), 1 failed")
    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert [e["event"] for e in events] == ["instruction", "step", "step", "final"]
    assert all("ts" in e for e in events)
    assert events[1]["tool"] == "write_file" and events[1]["args"]["path"] == "a.txt" and events[1]["ok"]
    assert len(events[2]["observation"]) <= MAX_LOGGED_OBSERVATION + 50


# 27
def test_session_log_recent_is_human_readable(tmp_path):
    log = SessionLog(Workspace(tmp_path))
    assert "no steps" in log.recent().lower()
    for i in range(12):
        log.step(Step(Action("read_file", {"path": f"f{i}.txt"}), i % 2 == 0, "ok"))
    lines = log.recent(3).splitlines()
    assert len(lines) == 3
    assert "f11.txt" in lines[-1] and lines[-1].startswith("✗")
    assert lines[-2].startswith("✓")
