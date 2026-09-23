"""Entry point: `python -m sonic [--root DIR] [--yolo] [--no-sandbox] [--max-steps N] [--planner stub|llm] [--web [--port N]]`."""
import argparse
import os
import sys
from pathlib import Path
from typing import Callable

from sonic.agent import Agent
from sonic.context import ContextGraph
from sonic.log import SessionLog
from sonic.planner import USAGE, Action, Planner, Step, StubPlanner
from sonic.undo import UndoStack
from sonic.workspace import Workspace

PROMPT = "sonic> "
SAFE_TOOLS = {"read_file", "list_dir"}
MAX_OBS_LINES = 20
SLASH_HELP = (
    "Slash commands:\n"
    "  /undo   revert the last file change (the replaced version goes to .sonic/trash)\n"
    "  /log    show recent steps from this session's log\n"
    "  /remember <fact>   save a note to your second brain (#tags and file names get linked)\n"
    "  /context <query>   show what the harness recalls for a query\n"
    "  /brain  summary of your context graph\n"
    "  /help   show this help\n"
    "  /quit   exit (also /exit, Ctrl-D)"
)


def _int_in(lo: int, hi: int | None = None) -> Callable[[str], int]:
    """argparse type: an integer in [lo, hi]."""
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid integer: {text!r}") from None
        if value < lo or (hi is not None and value > hi):
            bounds = f"between {lo} and {hi}" if hi is not None else f">= {lo}"
            raise argparse.ArgumentTypeError(f"must be {bounds}, got {value}")
        return value
    parse.__name__ = "int"
    return parse


def make_planner(kind: str = "stub", context: ContextGraph | None = None) -> Planner:
    """Single place to construct the planner."""
    if kind == "llm":
        from sonic.llm_planner import LLMPlanner
        return LLMPlanner.from_env(context=context.render_context if context else None)
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


def banner(ws: Workspace, yolo: bool, planner: str) -> str:
    sandbox = "on" if ws.sandboxed else "OFF (--no-sandbox)"
    approvals = "yolo" if yolo else "ask"
    return f"sonic · workspace {ws.root} · sandbox: {sandbox} · approvals: {approvals} · planner: {planner}"


def brain_summary(graph: ContextGraph) -> str:
    """Node counts by type and the most connected nodes."""
    data = graph.to_json()
    nodes = [n for n in data["nodes"] if not n.get("archived")]
    if not nodes:
        return "your second brain is empty: run something or /remember a fact"
    counts: dict[str, int] = {}
    for n in nodes:
        counts[n["type"]] = counts.get(n["type"], 0) + 1
    degree: dict[str, int] = {}
    for e in data["edges"]:
        for end in (e["src"], e["dst"]):
            degree[end] = degree.get(end, 0) + 1
    hubs = sorted(degree, key=lambda i: (-degree[i], i))[:5]
    parts = ", ".join(f"{v} {k}{'s' if v != 1 else ''}" for k, v in sorted(counts.items()))
    lines = [f"{len(nodes)} nodes ({parts}), {len(data['edges'])} links"]
    lines += [f"  hub: {h} ({degree[h]} link{"s" if degree[h] != 1 else ""})" for h in hubs]
    return "\n".join(lines)


def handle_slash(line: str, undo: UndoStack, log: SessionLog, graph: ContextGraph) -> bool:
    """Handle a slash command; return False when the REPL should exit."""
    parts = line.split(None, 1)
    cmd = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/quit", "/exit"):
        return False
    if cmd == "/undo":
        print(undo.undo())
    elif cmd == "/log":
        print(log.recent())
        print(f"(full log: {log.path})")
    elif cmd == "/remember":
        if not rest:
            print("usage: /remember <fact>")
        else:
            node = graph.remember(rest)
            graph.save()                 # never raises; warns once if it can't write
            linked = [n["id"] for n in graph.neighbors(node)]
            print(f"remembered {node}" + (f" → linked {', '.join(linked)}" if linked else ""))
    elif cmd == "/context":
        print(graph.render_context(rest or "") or "(nothing relevant yet)")
    elif cmd == "/brain":
        print(brain_summary(graph))
    elif cmd == "/help":
        print(USAGE)
        print(SLASH_HELP)
    else:
        print(f"unknown command {cmd} (try /help)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sonic", description="Supersonic coding harness")
    parser.add_argument("--root", default=".", help="workspace root (default: cwd)")
    parser.add_argument("--yolo", action="store_true", help="approve every action without asking")
    parser.add_argument("--no-sandbox", action="store_true", help="disable the OS sandbox for shell")
    parser.add_argument("--max-steps", type=_int_in(1), default=10, help="max steps per instruction")
    parser.add_argument("--planner", choices=["stub", "llm"], default="stub",
                        help="stub = rule-based (default); llm = Anthropic API (needs ANTHROPIC_API_KEY)")
    parser.add_argument("--web", action="store_true", help="open the local web UI instead of the REPL")
    parser.add_argument("--port", type=_int_in(1, 65535), default=8765,
                        help="web UI port (default 8765)")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser()
    if not root.is_dir():
        what = "is not a directory" if root.exists() else "does not exist"
        print(f"sonic: --root {args.root} {what}", file=sys.stderr)
        return 2
    ws = Workspace(root, sandboxed=not args.no_sandbox)
    graph = ContextGraph(ws)
    try:
        planner = make_planner(args.planner, graph)
    except RuntimeError as e:
        print(f"sonic: {e}", file=sys.stderr)
        return 2

    if args.web:
        from sonic.web import create_app
        try:
            app = create_app(ws, planner, port=args.port, yolo=args.yolo, max_steps=args.max_steps, context=graph)
        except OSError as e:
            reason = "port already in use" if e.errno in (48, 98, 10048) else (e.strerror or str(e))
            print(f"sonic: cannot start web UI on 127.0.0.1:{args.port}: {reason}", file=sys.stderr)
            return 2
        print(banner(ws, args.yolo, args.planner))
        print(f"web UI: {app.url}  (Ctrl-C to stop)")
        try:
            app.serve_forever()
        except KeyboardInterrupt:
            app.shutdown()
        return 0

    undo, log = UndoStack(ws), SessionLog(ws)

    def on_step(step: Step) -> None:
        print_step(step)
        log.step(step)

    agent = Agent(ws, planner, max_steps=args.max_steps,
                  approve=make_approver(args.yolo), on_step=on_step, undo=undo, context=graph)
    print(banner(ws, args.yolo, args.planner))
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
            if not handle_slash(line, undo, log, graph):
                return 0
            continue
        log.instruction(line)
        try:
            _, message = agent.run(line)
        except KeyboardInterrupt:
            message = "(cancelled)"
            print()
        except Exception as e:           # a planner/model failure must not kill the REPL
            message = f"error: {type(e).__name__}: {e}"
        log.final(message)
        print(message)


def _run() -> int:
    """main(), but a reader closing the pipe early (e.g. `| head -1`) ends quietly."""
    try:
        code = main()
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        # Point stdout at devnull so the interpreter's final flush doesn't fail again.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(_run())
