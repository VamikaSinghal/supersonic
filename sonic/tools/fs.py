"""Filesystem tools. [Owner: commit 2]

Every tool takes the Workspace first and returns a human-readable string
(the observation). Every failure raises ToolError (SandboxError for paths
outside the workspace); OS-level errors are converted, never leaked.
Writes into the harness's own `.sonic/` directory are refused; reads are allowed.
"""
import errno
import functools
import os
from pathlib import Path

from sonic.errors import ToolError
from sonic.workspace import Workspace, _short

MAX_READ_BYTES = 100_000

_ERRNO_TEXT = {
    errno.EACCES: "permission denied",
    errno.EPERM: "permission denied",
    errno.ENAMETOOLONG: "path too long",
    errno.ENOTDIR: "a parent is not a directory",
    errno.EISDIR: "is a directory",
    errno.ENOENT: "file not found",
    errno.ELOOP: "too many levels of symlinks",
    errno.ENOSPC: "no space left on device",
    errno.EROFS: "read-only filesystem",
}


def _tool(fn):
    """Convert any OSError/ValueError escaping `fn` into a ToolError naming the path."""
    @functools.wraps(fn)
    def wrapper(ws, *args, **kwargs):
        path = kwargs.get("path", args[0] if args else ".")
        try:
            return fn(ws, *args, **kwargs)
        except ToolError:
            raise
        except OSError as e:
            reason = _ERRNO_TEXT.get(e.errno) or e.strerror or type(e).__name__
            raise ToolError(f"{reason}: {_short(str(path))}") from None
        except ValueError as e:  # includes UnicodeError; e.g. NUL byte in a path
            raise ToolError(f"invalid path or content for {_short(str(path))!r}: {e}") from None
    return wrapper


def _read_text(ws: Workspace, path: str) -> str:
    """Return the full decoded text of a file. ToolError if missing, a directory, or binary."""
    p = ws.resolve(path)
    if p.is_dir():
        raise ToolError(f"is a directory: {path or '.'}")
    if not p.exists():
        raise ToolError(f"file not found: {path}")
    data = p.read_bytes()
    if b"\x00" in data:
        raise ToolError(f"binary file: {path}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"binary file (not UTF-8): {path}") from None


@_tool
def read_file(ws: Workspace, path: str) -> str:
    """Return file text. ToolError if missing, a directory, or binary. Truncate past MAX_READ_BYTES."""
    text = _read_text(ws, path)
    data = text.encode("utf-8")
    if len(data) > MAX_READ_BYTES:
        head = data[:MAX_READ_BYTES].decode("utf-8", errors="ignore")
        return f"{head}\n[truncated: showing {MAX_READ_BYTES} of {len(data)} bytes]"
    return text


@_tool
def list_dir(ws: Workspace, path: str = ".") -> str:
    """Newline-separated sorted entries; directories get a trailing '/'."""
    p = ws.resolve(path)
    if not p.is_dir():
        raise ToolError(f"not a directory: {path}")
    return "\n".join(sorted(e.name + "/" if e.is_dir() else e.name for e in p.iterdir()))


def _check_parents(ws: Workspace, p: Path, path: str) -> None:
    """ToolError if an existing ancestor of `p` (below root) is a file rather than a directory."""
    for parent in p.parents:
        if parent == ws.root or not parent.is_relative_to(ws.root):
            return
        if os.path.lexists(parent) and not parent.is_dir():
            rel = parent.relative_to(ws.root).as_posix()
            raise ToolError(f"cannot write {path}: {rel} is a file, not a directory")


@_tool
def write_file(ws: Workspace, path: str, content: str) -> str:
    """Create or overwrite a file, creating parent dirs. Returns e.g. 'wrote 12 bytes to a/b.txt'.

    ToolError if the path is a directory (including "" and "."), a parent is a
    file, or the path is inside the reserved `.sonic/`.
    """
    p = ws.resolve_writable(path)
    if p.is_dir():
        raise ToolError(f"is a directory: {path or '.'}")
    _check_parents(ws, p, path)
    data = content.encode("utf-8")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return f"wrote {len(data)} bytes to {p.relative_to(ws.root).as_posix()}"


@_tool
def edit_file(ws: Workspace, path: str, old: str, new: str) -> str:
    """Replace exactly one occurrence of `old` with `new`. ToolError if 0 or >1 matches.

    Line endings are preserved byte-for-byte and `old` may span lines. ToolError
    if `old` is empty or identical to `new`, the file is binary / not UTF-8, or
    the path is inside the reserved `.sonic/`.
    """
    p = ws.resolve_writable(path)
    text = _read_text(ws, path)
    if not old:
        raise ToolError(f"old string must not be empty (editing {path})")
    if old == new:
        raise ToolError(f"old and new strings are identical; nothing to change in {path}")
    count = text.count(old)
    if count == 0:
        raise ToolError(f"old string not found in {path}")
    if count > 1:
        raise ToolError(f"old string matched {count} times in {path}; must be unique")
    p.write_bytes(text.replace(old, new, 1).encode("utf-8"))
    return f"edited {p.relative_to(ws.root).as_posix()}"
