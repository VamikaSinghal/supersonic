"""OS-level sandbox for shell commands. [Owner: safety agent]

Policy (enforced by the kernel, so obfuscation like eval/base64/python -c can't bypass it):
  - No file or directory may be deleted anywhere, including inside the workspace.
    Only ws.scratch (the command's TMPDIR) allows deletion.
  - No file may be written outside the workspace, ws.scratch, or /dev.
  - Reads and process execution are allowed.

macOS: sandbox-exec with a generated SBPL profile.
Elsewhere: available() is False and run_shell refuses to run (fail closed)
unless the workspace was created with sandboxed=False.
"""
from sonic.workspace import Workspace


def available() -> bool:
    """True if this platform can enforce the policy."""
    raise NotImplementedError


def profile(ws: Workspace) -> str:
    """The sandbox profile text for this workspace."""
    raise NotImplementedError


def wrap(command: str, ws: Workspace) -> list[str]:
    """argv that runs `command` through /bin/sh inside the sandbox."""
    raise NotImplementedError


def env(ws: Workspace) -> dict[str, str]:
    """Environment for sandboxed commands: os.environ with TMPDIR (and TMP/TEMP) set to ws.scratch."""
    raise NotImplementedError
