"""Workspace: the directory the harness is allowed to touch. [Owner: commit 2]"""
import hashlib
import tempfile
from pathlib import Path

from sonic.errors import SandboxError, ToolError

# Harness-owned state (context graph, trash, session logs). Tools may read it but never write it.
RESERVED_DIR = ".sonic"


class Workspace:
    """The directory the harness may touch.

    Raises ValueError at construction if `root` does not exist or is not a
    directory (the message names the path), so callers can report it before
    any tool runs.
    """

    def __init__(self, root: str | Path, sandboxed: bool = True):
        try:
            resolved = Path(root).resolve()
            exists, is_dir = resolved.exists(), resolved.is_dir()
        except (OSError, ValueError, RuntimeError) as e:
            raise ValueError(f"invalid workspace root {str(root)!r}: {e}") from None
        if not exists:
            raise ValueError(f"workspace root does not exist: {root}")
        if not is_dir:
            raise ValueError(f"workspace root is not a directory: {root}")
        self.root = resolved
        self.sandboxed = sandboxed

    @property
    def scratch(self) -> Path:
        """Private temp dir for shell commands: the only place they may delete files."""
        tag = hashlib.sha1(str(self.root).encode()).hexdigest()[:10]
        path = Path(tempfile.gettempdir()).resolve() / f"sonic-scratch-{tag}"
        path.mkdir(exist_ok=True)
        return path

    def resolve(self, path: str) -> Path:
        """Return the absolute, symlink-resolved path for `path` (relative to root).

        Raises SandboxError if the result is outside root (traversal, absolute paths,
        or symlinks pointing outside), ToolError if the path is malformed (e.g. NUL byte).
        """
        try:
            resolved = (self.root / path).resolve()
        except (OSError, ValueError, RuntimeError) as e:
            raise ToolError(f"invalid path {_short(path)!r}: {e}") from None
        if not resolved.is_relative_to(self.root):
            raise SandboxError(f"path escapes workspace: {_short(path)}")
        return resolved

    def is_reserved(self, resolved: Path) -> bool:
        """True if an already-resolved path lies inside the harness's RESERVED_DIR.

        Compared case-insensitively so `.SONIC/x` is caught on case-insensitive
        filesystems (macOS); on case-sensitive ones refusing it is harmless.
        """
        parts = resolved.relative_to(self.root).parts
        return bool(parts) and parts[0].casefold() == RESERVED_DIR

    def resolve_writable(self, path: str) -> Path:
        """resolve(), plus ToolError if the target is inside the reserved RESERVED_DIR."""
        resolved = self.resolve(path)
        if self.is_reserved(resolved):
            raise ToolError(f"{_short(path)}: {RESERVED_DIR}/ is reserved for the harness; tools may not write there")
        return resolved


def _short(path: str, limit: int = 200) -> str:
    """`path` clipped for error messages."""
    return path if len(path) <= limit else path[:limit] + f"...[{len(path)} chars]"
