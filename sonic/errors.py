class ToolError(Exception):
    """A tool could not complete. Its message is fed back to the planner as an observation."""


class SandboxError(ToolError):
    """A path resolved outside the workspace root."""
