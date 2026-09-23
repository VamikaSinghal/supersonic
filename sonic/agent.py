"""The agent loop: plan -> approve -> execute -> observe -> repeat. [Owner: commit 5]"""
from typing import Callable

from sonic.planner import Action, Done, Planner, Step
from sonic.tools import fs, shell
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
    ):
        ...

    def run(self, instruction: str) -> tuple[list[Step], str]:
        """Loop until the planner returns Done or max_steps is hit.

        Returns (steps, final_message). Tool errors (ToolError, unknown tool,
        bad args) and denied approvals become ok=False steps, never crashes.
        """
        raise NotImplementedError
