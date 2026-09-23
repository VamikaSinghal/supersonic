"""REPL wired to the agent, with approval prompts."""
import subprocess
import sys


def sonic(tmp_path, stdin, *flags):
    return subprocess.run(
        [sys.executable, "-m", "sonic", "--root", str(tmp_path), *flags],
        input=stdin, capture_output=True, text=True, timeout=20,
    ).stdout


# 15
def test_repl_yolo_executes_instructions(tmp_path):
    out = sonic(tmp_path, "create a.txt with hi then run cat a.txt\n/quit\n", "--yolo")
    assert (tmp_path / "a.txt").read_text() == "hi"
    assert "sandbox: on" in out
    assert "✓ write_file" in out and "✓ run_shell" in out and "completed 2 step(s)" in out


# 16
def test_repl_asks_before_writes_but_not_reads(tmp_path):
    (tmp_path / "r.txt").write_text("readme")
    out = sonic(tmp_path, "read r.txt\ncreate a.txt with hi\nn\n")
    assert "readme" in out                      # read ran without a prompt
    assert "denied by user" in out
    assert not (tmp_path / "a.txt").exists()


# 17
def test_repl_always_approves_rest_of_session_and_handles_help(tmp_path):
    out = sonic(tmp_path, "/help\ncreate a.txt with 1\na\ncreate b.txt with 2\nmake a sandwich\n")
    assert (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").exists()
    assert out.count("[y/n/a]") == 1           # 'a' = always, so b.txt wasn't prompted
    assert out.lower().count("run <command>") >= 2   # /help and unknown input both show help


# 32
def test_repl_undo_moves_created_file_to_trash(tmp_path):
    out = sonic(tmp_path, "create a.txt with hi\n/undo\n/undo\n", "--yolo")
    assert not (tmp_path / "a.txt").exists()
    assert (tmp_path / ".sonic" / "trash").exists()
    assert "nothing to undo" in out.lower()


# 33
def test_repl_log_command_and_session_file(tmp_path):
    out = sonic(tmp_path, "create a.txt with hi\n/log\n", "--yolo")
    assert "✓ write_file" in out.split("/log")[-1] or out.count("write_file") >= 2
    logs = list((tmp_path / ".sonic" / "sessions").glob("*.jsonl"))
    assert len(logs) == 1 and '"instruction"' in logs[0].read_text()


# 34
def test_repl_llm_planner_without_key_explains_and_exits(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = subprocess.run([sys.executable, "-m", "sonic", "--root", str(tmp_path), "--planner", "llm"],
                       input="", capture_output=True, text=True, timeout=20)
    assert r.returncode != 0
    assert "ANTHROPIC_API_KEY" in r.stdout + r.stderr
