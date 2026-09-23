"""Planners decide the next action. [Owner: commit 4]

The Agent only depends on the Planner protocol, so an LLM-backed planner
can replace StubPlanner without touching the loop.
"""
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol


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
        actions = _parse(instruction)
        if actions is None:
            return Done(HELP)
        if len(history) < len(actions):
            return actions[len(history)]
        failed = sum(1 for s in history if not s.ok)
        msg = f"completed {len(history)} step(s)"
        return Done(f"{msg}, {failed} failed" if failed else msg)


HELP = (
    "Sorry, I didn't understand. Supported commands (chain with 'then'):\n"
    "  read <path>\n"
    "  list [<path>]\n"
    "  create <path> with <content>\n"
    "  write <content> to <path>\n"
    "  replace <old> with <new> in <path>\n"
    "  run <command>"
)

_SPLIT = re.compile(r"\s+(?:and\s+)?then\s+", re.IGNORECASE)
_PATH = r"(?P<path>'[^']+'|\"[^\"]+\"|\S+)"


def _unquote(s: str) -> str:
    """Strip one layer of matching quotes, unless that quote also appears inside."""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"" and s[0] not in s[1:-1]:
        return s[1:-1]
    return s


def _text(s: str) -> str:
    """Unquote free text and expand literal \\n escapes."""
    return _unquote(s).replace("\\n", "\n")


def _rule(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE | re.DOTALL)


_RULES: list[tuple[re.Pattern[str], Callable[[re.Match[str]], Action]]] = [
    (_rule(r"(?:read|show|cat|open)\s+" + _PATH),
     lambda m: Action("read_file", {"path": _unquote(m["path"])})),
    (_rule(r"(?:list|ls)(?:\s+" + _PATH + ")?"),
     lambda m: Action("list_dir", {"path": _unquote(m["path"] or ".")})),
    (_rule(r"replace\s+(?P<old>.+?)\s+with\s+(?P<new>.+)\s+in\s+" + _PATH),
     lambda m: Action("edit_file", {"path": _unquote(m["path"]),
                                    "old": _text(m["old"]), "new": _text(m["new"])})),
    (_rule(r"(?:create|write)\s+" + _PATH + r"\s+with\s+(?P<content>.*)"),
     lambda m: Action("write_file", {"path": _unquote(m["path"]), "content": _text(m["content"])})),
    (_rule(r"write\s+(?P<content>.*)\s+to\s+" + _PATH),
     lambda m: Action("write_file", {"path": _unquote(m["path"]), "content": _text(m["content"])})),
    (_rule(r"(?:run|exec|execute)\s+(?P<command>.+)"),
     lambda m: Action("run_shell", {"command": m["command"].strip()})),
]


def _parse(instruction: str) -> list[Action] | None:
    """Turn an instruction into actions; None if any clause is unrecognised."""
    actions = []
    for clause in _SPLIT.split(instruction.strip()):
        for pattern, build in _RULES:
            if m := pattern.fullmatch(clause.strip()):
                actions.append(build(m))
                break
        else:
            return None
    return actions or None
