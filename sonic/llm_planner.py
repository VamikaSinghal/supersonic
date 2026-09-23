"""LLM-backed planner using the Anthropic Messages API tool-use format. [Owner: step 9]

Drop-in replacement for StubPlanner: implements Planner.next_action.
The HTTP call is injected as `client(request_body) -> response_body`, so tests
use a fake and production uses `from_env()` (stdlib urllib, ANTHROPIC_API_KEY).
"""
import json
import os
import urllib.error
import urllib.request
from typing import Callable

from sonic.planner import Action, Done, Step

DEFAULT_MODEL = "claude-opus-5-5"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
HTTP_TIMEOUT = 120

SYSTEM_PROMPT = """\
You are Supersonic's planner: a coding agent working inside a single project workspace.
Act through the provided tools, one tool call per turn; you will see each result before choosing the next step.
All paths are relative to the workspace root; you cannot reach files outside it.
Never try to delete files or directories: deletion is disabled by policy, deletion commands are refused, and the shell sandbox blocks it anyway. Overwrite or edit files instead.
Read a file before editing it, and keep edits minimal.
When the task is done (or cannot be done), stop calling tools and reply with a short plain-text summary of what you did."""

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "read_file",
        "description": (
            "Read a UTF-8 text file from the workspace and return its contents. "
            "Fails for missing files, directories, and binary files. Very large files are truncated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path, e.g. 'src/app.py'."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": (
            "List a workspace directory: one sorted entry per line, directories end with '/'. "
            "Not recursive."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative directory path. Defaults to '.' (the root)."},
            },
            "required": [],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create a file or overwrite it entirely with `content`, creating parent directories. "
            "For small changes to an existing file prefer edit_file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path."},
                "content": {"type": "string", "description": "The complete new file contents."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace exactly one occurrence of `old` with `new` in a text file. "
            "`old` must match the file byte-for-byte (including whitespace and indentation) and must be "
            "unique in the file; the edit fails if it matches zero or several times, so include enough "
            "surrounding lines to make it unique. Read the file first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path."},
                "old": {"type": "string", "description": "Exact existing text to replace; must occur exactly once."},
                "new": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old", "new"],
        },
    },
    {
        "name": "run_shell",
        "description": (
            "Run a shell command with the workspace root as the working directory and return the exit "
            "code, stdout and stderr (long output is truncated). Runs in a sandbox where deleting files "
            "is impossible; deletion commands (rm, rmdir, unlink, find -delete, git clean, ...) and other "
            "dangerous commands such as sudo are refused. Use it for tests, builds, git status, grep, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."},
                "timeout": {"type": "number", "description": "Seconds before the command is killed (default 30)."},
            },
            "required": ["command"],
        },
    },
]


def _http_client(api_key: str) -> Callable[[dict], dict]:
    """POST a Messages API body with urllib; RuntimeError on HTTP or network errors."""
    def call(body: dict) -> dict:
        req = urllib.request.Request(
            API_URL,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "x-api-key": api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            snippet = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Anthropic API error {e.code}: {snippet}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Anthropic API request failed: {e.reason}") from e
    return call


class LLMPlanner:
    def __init__(self, client: Callable[[dict], dict], model: str = DEFAULT_MODEL, max_tokens: int = 16000):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    @classmethod
    def from_env(cls) -> "LLMPlanner":
        """Real client via urllib. RuntimeError mentioning ANTHROPIC_API_KEY if it is unset."""
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set; export it to use the LLM planner")
        return cls(_http_client(key), model=os.environ.get("SONIC_MODEL") or DEFAULT_MODEL)

    def build_request(self, instruction: str, history: list[Step]) -> dict:
        """Messages API body: model, max_tokens, system, tools, messages.

        messages = [user: instruction] then, per past step i, an assistant turn with a
        tool_use block (id f"toolu_{i}") and a user turn with the matching tool_result
        (content = observation, is_error = not ok).
        """
        messages: list[dict] = [{"role": "user", "content": instruction}]
        for i, step in enumerate(history):
            tool_id = f"toolu_{i}"
            messages.append({"role": "assistant", "content": [
                {"type": "tool_use", "id": tool_id, "name": step.action.tool, "input": step.action.args},
            ]})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id,
                 "content": step.observation, "is_error": not step.ok},
            ]})
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": SYSTEM_PROMPT,
            "tools": TOOL_SCHEMAS,
            "messages": messages,
        }

    def next_action(self, instruction: str, history: list[Step]) -> Action | Done:
        """First tool_use block -> Action(name, input). No tool_use -> Done(joined text)."""
        response = self.client(self.build_request(instruction, history))
        blocks = response.get("content") or []
        for block in blocks:
            if block.get("type") == "tool_use":
                return Action(block["name"], dict(block.get("input") or {}))
        text = "\n".join(b["text"] for b in blocks if b.get("type") == "text" and b.get("text")).strip()
        if not text and response.get("stop_reason") == "refusal":
            return Done("(model refused the request)")
        return Done(text or "(no response)")
