"""Planners decide the next action. [Owner: commit 4]

The Agent only depends on the Planner protocol, so an LLM-backed planner
can replace StubPlanner without touching the loop.
"""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Action:
    tool: str               # one of: read_file, list_dir, write_file, edit_file, run_shell
    args: dict = field(default_factory=dict)


@dataclass
class Done:
    message: str


@dataclass
class Step:
    action: Action
    ok: bool
    observation: str


class Planner(Protocol):
    def next_action(self, instruction: str, history: list[Step]) -> Action | Done: ...


class StubPlanner:
    """Rule-based stand-in for a model.

    Understands (case-insensitive), chainable with ' then ' / ' and then ':
      read <path> | show <path> | cat <path>
      list [<path>] | ls [<path>]
      create <path> with <content> | write <content> to <path>
      replace <old> with <new> in <path>
      run <command>
    Returns actions one at a time based on len(history); returns Done when finished.
    Unrecognised instructions return Done with a help message.
    """

    def next_action(self, instruction: str, history: list[Step]) -> Action | Done:
        raise NotImplementedError
