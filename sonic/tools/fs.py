"""Filesystem tools. [Owner: commit 2]

Every tool takes the Workspace first and returns a human-readable string
(the observation). Failures raise ToolError.
"""
from sonic.workspace import Workspace

MAX_READ_BYTES = 100_000


def read_file(ws: Workspace, path: str) -> str:
    """Return file text. ToolError if missing, a directory, or binary. Truncate past MAX_READ_BYTES."""
    raise NotImplementedError


def list_dir(ws: Workspace, path: str = ".") -> str:
    """Newline-separated sorted entries; directories get a trailing '/'."""
    raise NotImplementedError


def write_file(ws: Workspace, path: str, content: str) -> str:
    """Create or overwrite a file, creating parent dirs. Returns e.g. 'wrote 12 bytes to a/b.txt'."""
    raise NotImplementedError


def edit_file(ws: Workspace, path: str, old: str, new: str) -> str:
    """Replace exactly one occurrence of `old` with `new`. ToolError if 0 or >1 matches."""
    raise NotImplementedError
