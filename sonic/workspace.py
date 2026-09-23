"""Workspace: the directory the harness is allowed to touch. [Owner: commit 2]"""
from pathlib import Path

from sonic.errors import SandboxError


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def resolve(self, path: str) -> Path:
        """Return the absolute, symlink-resolved path for `path` (relative to root).

        Raises SandboxError if the result is outside root (traversal, absolute paths,
        or symlinks pointing outside).
        """
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise SandboxError(f"path escapes workspace: {path}")
        return resolved
