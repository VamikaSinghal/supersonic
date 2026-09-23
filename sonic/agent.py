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
        A run_shell step is ok=False when the command exits non-zero or times out."""
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
        """
        steps: list[Step] = []
        task = self._remember(lambda g: g.start_task(instruction))
        try:
            while True:
                decision = self.planner.next_action(instruction, steps)
                if isinstance(decision, Done):
                    return steps, decision.message
                if len(steps) >= self.max_steps:
                    return steps, f"stopped: hit max steps ({self.max_steps})"
                step = self._execute(decision)
                steps.append(step)
                if task is not None:
                    self._remember(lambda g: g.record_step(task, step))
                self.on_step(step)
        finally:
            self._remember(lambda g: g.save())

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
        tool = TOOLS.get(action.tool)
        if tool is None:
            return Step(action, False, f"unknown tool: {action.tool}")
        try:
            inspect.signature(tool).bind(self.workspace, **action.args)
        except TypeError as e:
            return Step(action, False, f"error: bad arguments for {action.tool}: {e}")
        except ValueError:
            pass  # signature unavailable; let the call itself decide
        if not self.approve(action):
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
        return Step(action, ok, str(result))
