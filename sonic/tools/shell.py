"""Shell tool. [Owner: commit 3]"""
from dataclasses import dataclass

from sonic.workspace import Workspace

DEFAULT_TIMEOUT = 30
MAX_OUTPUT_CHARS = 20_000


@dataclass
class ShellResult:
    exit_code: int | None  # None if timed out
    stdout: str
    stderr: str
    timed_out: bool = False

    def __str__(self) -> str:
        """Observation text for the planner: exit code (or 'timed out'), stdout, stderr."""
        raise NotImplementedError


def run_shell(ws: Workspace, command: str, timeout: float = DEFAULT_TIMEOUT) -> ShellResult:
    """Run `command` via the shell with cwd=ws.root.

    Raises ToolError for denylisted commands (e.g. 'rm -rf /', 'sudo', 'mkfs', fork bombs).
    Kills the process on timeout. Truncates stdout/stderr past MAX_OUTPUT_CHARS.
    """
    raise NotImplementedError
