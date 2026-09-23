"""LLM-backed planner using the Anthropic Messages API tool-use format. [Owner: step 9]

Drop-in replacement for StubPlanner: implements Planner.next_action.
The HTTP call is injected as `client(request_body) -> response_body`, so tests
use a fake and production uses `from_env()` (stdlib urllib, ANTHROPIC_API_KEY).
"""
from typing import Callable

from sonic.planner import Action, Done, Step

DEFAULT_MODEL = "claude-opus-5-5"
SYSTEM_PROMPT = "..."  # describe the harness, the workspace, and the no-deletion policy

# Anthropic tool definitions, one per entry in sonic.agent.TOOLS:
# {"name": ..., "description": ..., "input_schema": {"type": "object", "properties": ..., "required": [...]}}
TOOL_SCHEMAS: list[dict] = []


class LLMPlanner:
    def __init__(self, client: Callable[[dict], dict], model: str = DEFAULT_MODEL, max_tokens: int = 1024):
        ...

    @classmethod
    def from_env(cls) -> "LLMPlanner":
        """Real client via urllib. RuntimeError mentioning ANTHROPIC_API_KEY if it is unset."""
        raise NotImplementedError

    def build_request(self, instruction: str, history: list[Step]) -> dict:
        """Messages API body: model, max_tokens, system, tools, messages.

        messages = [user: instruction] then, per past step i, an assistant turn with a
        tool_use block (id f"toolu_{i}") and a user turn with the matching tool_result
        (content = observation, is_error = not ok).
        """
        raise NotImplementedError

    def next_action(self, instruction: str, history: list[Step]) -> Action | Done:
        """First tool_use block -> Action(name, input). No tool_use -> Done(joined text)."""
        raise NotImplementedError
