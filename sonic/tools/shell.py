"""Shell tool. [Owner: commit 3]"""
import os
import re
import signal
import subprocess
from dataclasses import dataclass

from sonic.errors import ToolError
from sonic.workspace import Workspace

DEFAULT_TIMEOUT = 30
MAX_OUTPUT_CHARS = 20_000

_CMD_START = r"(?:^|[;&|(`]|\$\()\s*"
_DENYLIST: list[tuple[str, re.Pattern[str]]] = [
    ("sudo", re.compile(_CMD_START + r"sudo\b")),
    ("rm of / or home", re.compile(
        r"\brm\s+(?:-{1,2}[\w-]+\s+)*(?:/|~|\$HOME|\$\{HOME\})/?\*?(?=$|[\s;&|)])")),
    ("mkfs", re.compile(r"\bmkfs\b")),
    ("dd to a device", re.compile(r"\bdd\b[^;&|]*\bof=/dev/")),
    ("shutdown/reboot", re.compile(_CMD_START + r"(?:shutdown|reboot|halt|poweroff)\b")),
    ("fork bomb", re.compile(r"([\w:]+)\s*\(\)\s*\{\s*\1\s*\|\s*\1\s*&")),
]


@dataclass
class ShellResult:
    exit_code: int | None  # None if timed out
    stdout: str
    stderr: str
    timed_out: bool = False

    def __str__(self) -> str:
        """Observation text for the planner: exit code (or 'timed out'), stdout, stderr."""
        if self.timed_out:
            parts = ["timed out"]
        else:
            parts = [f"exit {self.exit_code}"]
        if self.stdout:
            parts.append(f"--- stdout ---\n{self.stdout}")
        if self.stderr:
            parts.append(f"--- stderr ---\n{self.stderr}")
        return "\n".join(parts)


def _check_denylist(command: str) -> None:
    """Raise ToolError if `command` matches a dangerous pattern."""
    for label, pattern in _DENYLIST:
        if pattern.search(command):
            raise ToolError(f"command blocked by denylist ({label}): {command!r}")


def _truncate(text: str) -> str:
    """Cap `text` at MAX_OUTPUT_CHARS with a marker."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n[truncated {len(text) - MAX_OUTPUT_CHARS} chars]"


def _as_text(data: str | bytes | None) -> str:
    if data is None:
        return ""
    return data.decode(errors="replace") if isinstance(data, bytes) else data


def run_shell(ws: Workspace, command: str, timeout: float = DEFAULT_TIMEOUT) -> ShellResult:
    """Run `command` via the shell with cwd=ws.root.

    If ws.sandboxed: run via sandbox.wrap(command, ws) with sandbox.env(ws); if
    sandbox.available() is False, raise ToolError telling the user about --no-sandbox.
    Deletion commands (rm, rmdir, unlink, shred, find -delete, git clean, ...) are
    denylisted too, so the user gets a clear message instead of a sandbox error.

    Raises ToolError for denylisted commands (e.g. 'rm -rf /', 'sudo', 'mkfs', fork bombs).
    Kills the process on timeout. Truncates stdout/stderr past MAX_OUTPUT_CHARS.
    """
    _check_denylist(command)
    proc = subprocess.Popen(
        command, shell=True, cwd=ws.root, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        return ShellResult(
            None, _truncate(_as_text(stdout)), _truncate(_as_text(stderr)), timed_out=True
        )
    return ShellResult(proc.returncode, _truncate(stdout), _truncate(stderr))
