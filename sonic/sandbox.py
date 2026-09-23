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
import os
import sys
from pathlib import Path

from sonic.workspace import Workspace

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def available() -> bool:
    """True if this platform can enforce the policy."""
    return sys.platform == "darwin" and os.path.exists(SANDBOX_EXEC)


def _lit(path: Path | str) -> str:
    """`path` resolved and quoted as an SBPL string literal."""
    real = os.path.realpath(path)
    return '"' + real.replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile(ws: Workspace) -> str:
    """The sandbox profile text for this workspace."""
    root, scratch = _lit(ws.root), _lit(ws.scratch)
    return "\n".join([
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f"(allow file-write* (subpath {root}) (subpath {scratch}) (subpath \"/dev\"))",
        "(deny file-write-unlink)",
        f"(allow file-write-unlink (subpath {scratch}))",
    ])


def wrap(command: str, ws: Workspace) -> list[str]:
    """argv that runs `command` through /bin/sh inside the sandbox."""
    return [SANDBOX_EXEC, "-p", profile(ws), "/bin/sh", "-c", command]


def env(ws: Workspace) -> dict[str, str]:
    """Environment for sandboxed commands: os.environ with TMPDIR (and TMP/TEMP) set to ws.scratch."""
    scratch = str(ws.scratch)
    return {**os.environ, "TMPDIR": scratch, "TMP": scratch, "TEMP": scratch}
