"""Workspace: the directory the harness is allowed to touch. [Owner: commit 2]"""
from pathlib import Path


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def resolve(self, path: str) -> Path:
        """Return the absolute, symlink-resolved path for `path` (relative to root).

        Raises SandboxError if the result is outside root (traversal, absolute paths,
        or symlinks pointing outside).
        """
        raise NotImplementedError
