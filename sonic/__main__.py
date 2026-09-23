"""REPL entry point: `python -m sonic [--root DIR] [--yolo] [--no-sandbox] [--max-steps N]`."""
import argparse
import sys
from typing import Callable

from sonic.agent import Agent
from sonic.planner import HELP, Action, Planner, Step, StubPlanner
from sonic.workspace import Workspace

PROMPT = "sonic> "
SAFE_TOOLS = {"read_file", "list_dir"}
MAX_OBS_LINES = 20
SLASH_HELP = "Slash commands:\n  /help   show this help\n  /quit   exit (also /exit, Ctrl-D)"


def make_planner() -> Planner:
    """Single place to construct the planner (swap in an LLM planner here)."""
    return StubPlanner()


def preview(action: Action) -> str:
    """One-line summary of an action for the approval prompt."""
    a = action.args
    if action.tool == "write_file":
        size = len(str(a.get("content", "")).encode())
        return f"write_file {a.get('path')} ({size} bytes)"
    if action.tool == "edit_file":
        return f"edit_file {a.get('path')}: {a.get('old')!r} → {a.get('new')!r}"
    if action.tool == "run_shell":
        return f"run_shell: {a.get('command')}"
    return f"{action.tool} {a}"


def make_approver(yolo: bool) -> Callable[[Action], bool]:
    """Build an approve callback; 'a' switches to approve-all for the session."""
    always = yolo

    def approve(action: Action) -> bool:
        nonlocal always
        if always or action.tool in SAFE_TOOLS:
            return True
        print(preview(action))
        try:
            answer = input("  allow? [y/n/a] ").strip().lower()
        except EOFError:
            print()
            return False
        if answer == "a":
            always = True
            return True
        return answer == "y"

    return approve


def print_step(step: Step) -> None:
    """Print a step's status and its observation, truncated."""
    mark = "✓" if step.ok else "✗"
    lines = step.observation.splitlines() or [""]
    print(f"{mark} {step.action.tool} → {lines[0]}")
    rest = lines[1:]
    for line in rest[:MAX_OBS_LINES]:
        print(f"    {line}")
    if len(rest) > MAX_OBS_LINES:
        print(f"    … ({len(rest) - MAX_OBS_LINES} more lines)")


def banner(ws: Workspace, yolo: bool) -> str:
    sandbox = "on" if ws.sandboxed else "OFF (--no-sandbox)"
    approvals = "yolo" if yolo else "ask"
    return f"sonic · workspace {ws.root} · sandbox: {sandbox} · approvals: {approvals}"


def handle_slash(line: str) -> bool:
    """Handle a slash command; return False when the REPL should exit."""
    cmd = line.split()[0].lower()
    if cmd in ("/quit", "/exit"):
        return False
    if cmd == "/help":
        print(HELP.removeprefix("Sorry, I didn't understand. "))
        print(SLASH_HELP)
    else:
        print(f"unknown command {cmd} (try /help)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sonic", description="Supersonic coding harness")
    parser.add_argument("--root", default=".", help="workspace root (default: cwd)")
    parser.add_argument("--yolo", action="store_true", help="approve every action without asking")
    parser.add_argument("--no-sandbox", action="store_true", help="disable the OS sandbox for shell")
    parser.add_argument("--max-steps", type=int, default=10, help="max steps per instruction")
    args = parser.parse_args(argv)

    ws = Workspace(args.root, sandboxed=not args.no_sandbox)
    agent = Agent(ws, make_planner(), max_steps=args.max_steps,
                  approve=make_approver(args.yolo), on_step=print_step)
    print(banner(ws, args.yolo))
    print("Type an instruction, /help, or /quit.")

    while True:
        try:
            line = input(PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.startswith("/"):
            if not handle_slash(line):
                return 0
            continue
        try:
            _, message = agent.run(line)
        except KeyboardInterrupt:
            print("\n(cancelled)")
            continue
        print(message)


if __name__ == "__main__":
    sys.exit(main())
