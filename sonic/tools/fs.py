"""Filesystem tools. [Owner: commit 2]

Every tool takes the Workspace first and returns a human-readable string
(the observation). Failures raise ToolError.
"""
from sonic.errors import ToolError
from sonic.workspace import Workspace

MAX_READ_BYTES = 100_000


def _read_text(ws: Workspace, path: str) -> str:
    """Return the full decoded text of a file. ToolError if missing, a directory, or binary."""
    p = ws.resolve(path)
    if not p.exists():
        raise ToolError(f"file not found: {path}")
    if p.is_dir():
        raise ToolError(f"is a directory: {path}")
    data = p.read_bytes()
    if b"\x00" in data:
        raise ToolError(f"binary file: {path}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"binary file (not UTF-8): {path}")


def read_file(ws: Workspace, path: str) -> str:
    """Return file text. ToolError if missing, a directory, or binary. Truncate past MAX_READ_BYTES."""
    text = _read_text(ws, path)
    data = text.encode("utf-8")
    if len(data) > MAX_READ_BYTES:
        head = data[:MAX_READ_BYTES].decode("utf-8", errors="ignore")
        return f"{head}\n[truncated: showing {MAX_READ_BYTES} of {len(data)} bytes]"
    return text


def list_dir(ws: Workspace, path: str = ".") -> str:
    """Newline-separated sorted entries; directories get a trailing '/'."""
    p = ws.resolve(path)
    if not p.is_dir():
        raise ToolError(f"not a directory: {path}")
    return "\n".join(sorted(e.name + "/" if e.is_dir() else e.name for e in p.iterdir()))


def write_file(ws: Workspace, path: str, content: str) -> str:
    """Create or overwrite a file, creating parent dirs. Returns e.g. 'wrote 12 bytes to a/b.txt'."""
    p = ws.resolve(path)
    if p.is_dir():
        raise ToolError(f"is a directory: {path}")
    p.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    p.write_bytes(data)
    return f"wrote {len(data)} bytes to {p.relative_to(ws.root).as_posix()}"


def edit_file(ws: Workspace, path: str, old: str, new: str) -> str:
    """Replace exactly one occurrence of `old` with `new`. ToolError if 0 or >1 matches."""
    text = _read_text(ws, path)
    p = ws.resolve(path)
    count = text.count(old) if old else 0
    if count == 0:
        raise ToolError(f"old string not found in {path}")
    if count > 1:
        raise ToolError(f"old string matched {count} times in {path}; must be unique")
    p.write_bytes(text.replace(old, new, 1).encode("utf-8"))
    return f"edited {p.relative_to(ws.root).as_posix()}"
