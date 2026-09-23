"""The agent loop: plan -> approve -> execute -> observe -> repeat. [Owner: commit 5]"""
import inspect
import sys
from typing import Callable

from sonic.context import ContextGraph
from sonic.errors import ToolError
from sonic.planner import Action, Done, Planner, Step
from sonic.tools import fs, shell
from sonic.undo import UndoStack
from sonic.workspace import Workspace

TOOLS: dict[str, Callable] = {
    "read_file": fs.read_file,
    "list_dir": fs.list_dir,
    "write_file": fs.write_file,
    "edit_file": fs.edit_file,
    "run_shell": shell.run_shell,
}


class Agent:
    def __init__(
        self,
        workspace: Workspace,
        planner: Planner,
        max_steps: int = 10,
        approve: Callable[[Action], bool] = lambda action: True,
        on_step: Callable[[Step], None] = lambda step: None,
        undo: UndoStack | None = None,
        context: ContextGraph | None = None,
    ):
        """undo: if given, write_file/edit_file changes are recorded after they succeed.
        context: if given, every run and step is captured into the graph and saved.
        A run_shell step is ok=False when the command exits non-zero or times out.
        max_steps=0 returns at once without consulting the planner; negative is a ValueError."""
        if isinstance(max_steps, bool) or not isinstance(max_steps, int):
            raise TypeError(f"max_steps must be an int, got {type(max_steps).__name__}")
        if max_steps < 0:
            raise ValueError(f"max_steps must be >= 0, got {max_steps}")
        self.workspace = workspace
        self.planner = planner
        self.max_steps = max_steps
        self.approve = approve
        self.on_step = on_step
        self.undo = undo
        self.context = context

    def run(self, instruction: str) -> tuple[list[Step], str]:
        """Loop until the planner returns Done or max_steps is hit.

        Returns (steps, final_message). Tool errors (ToolError, unknown tool,
        bad args) and denied approvals become ok=False steps, never crashes.
        A planner that raises (e.g. a network failure) or returns something that
        is neither an Action nor Done ends the run with a "planner error: ..."
        message; KeyboardInterrupt still propagates. The context graph is saved
        either way.
        """
        steps: list[Step] = []
        task = self._remember(lambda g: g.start_task(instruction))
        try:
            while True:
                if self.max_steps == 0:
                    return steps, self._max_steps_message()
                try:
                    decision = self.planner.next_action(instruction, steps)
                except Exception as e:
                    return steps, f"planner error: {type(e).__name__}: {e}"
                if isinstance(decision, Done):
                    return steps, str(decision.message)
                if not isinstance(decision, Action):
                    return steps, f"planner error: expected an Action or Done, got {type(decision).__name__}: {decision!r:.200}"
                if len(steps) >= self.max_steps:
                    return steps, self._max_steps_message()
                step = self._execute(decision)
                steps.append(step)
                if task is not None:
                    self._remember(lambda g: g.record_step(task, step))
                try:
                    self.on_step(step)
                except Exception as e:
                    print(f"warning: on_step callback: {type(e).__name__}: {e}", file=sys.stderr)
        finally:
            self._remember(lambda g: g.save())

    def _max_steps_message(self) -> str:
        return f"stopped: hit max steps ({self.max_steps})"

    def _remember(self, fn: Callable[[ContextGraph], object]) -> object:
        """Apply fn to the context graph; a graph failure must never break a run."""
        if self.context is None:
            return None
        try:
            return fn(self.context)
        except Exception as e:
            print(f"warning: context graph: {type(e).__name__}: {e}", file=sys.stderr)
            return None

    def _execute(self, action: Action) -> Step:
        """Run one action, turning every failure into an ok=False step."""
        tool = TOOLS.get(action.tool) if isinstance(action.tool, str) else None
        if tool is None:
            return Step(action, False, f"unknown tool: {action.tool!s:.200}")
        if not isinstance(action.args, dict):
            return Step(action, False, f"error: bad arguments for {action.tool}: "
                                       f"expected a dict, got {type(action.args).__name__}")
        try:
            inspect.signature(tool).bind(self.workspace, **action.args)
        except TypeError as e:
            return Step(action, False, f"error: bad arguments for {action.tool}: {e}")
        except ValueError:
            pass  # signature unavailable; let the call itself decide
        try:
            approved = self.approve(action)
        except Exception as e:
            return Step(action, False, f"denied (approval failed: {type(e).__name__}: {e})")
        if not approved:
            return Step(action, False, "denied by user")
        change = self.undo.snapshot(action) if self.undo is not None else None
        try:
            result = tool(self.workspace, **action.args)
        except ToolError as e:
            return Step(action, False, f"error: {e}")
        except Exception as e:
            return Step(action, False, f"error: {type(e).__name__}: {e}")
        ok = not (isinstance(result, shell.ShellResult) and (result.timed_out or result.exit_code != 0))
        if ok and change is not None and self.undo is not None:
            self.undo.push(change)
        return Step(action, ok, _observation(result))


def _observation(result: object) -> str:
    """Tool results become strings: None -> "", bytes decoded, anything else str()."""
    if result is None:
        return ""
    if isinstance(result, bytes):
        return result.decode("utf-8", errors="replace")
    return result if isinstance(result, str) else str(result)
