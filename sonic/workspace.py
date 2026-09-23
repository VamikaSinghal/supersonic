"""Workspace: the directory the harness is allowed to touch. [Owner: commit 2]"""
import hashlib
import tempfile
from pathlib import Path

from sonic.errors import SandboxError


class Workspace:
    def __init__(self, root: str | Path, sandboxed: bool = True):
        self.root = Path(root).resolve()
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
        or symlinks pointing outside).
        """
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise SandboxError(f"path escapes workspace: {path}")
        return resolved
