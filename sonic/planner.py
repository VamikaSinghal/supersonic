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
    Paths may contain spaces unquoted, or be quoted. Chaining only splits
    outside quotes and before a command verb, so quote to keep ' then ' literal.
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


USAGE = (
    "Supported commands (chain with 'then'):\n"
    "  read <path>\n"
    "  list [<path>]\n"
    "  create <path> with <content>\n"
    "  write <content> to <path>\n"
    "  replace <old> with <new> in <path>\n"
    "  run <command>\n"
    "Paths may contain spaces; wrap text in quotes to keep a literal ' then '."
)
HELP = "Sorry, I didn't understand. " + USAGE

_VERBS = r"read|show|cat|open|list|ls|create|write|replace|run|exec|execute"
_THEN = re.compile(r"\s++(?:and\s++)?then\s++(?=(?:" + _VERBS + r")\b)", re.IGNORECASE)
_QUOTED = r"'[^']+'|\"[^\"]+\""
_ANY_PATH = r"(?P<path>.+)"
_PATH = r"(?P<path>" + _QUOTED + r"|[^'\"]+)"
_LAZY_PATH = r"(?P<path>" + _QUOTED + r"|[^'\"]+?)"


def _split(instruction: str) -> list[str]:
    """Split on ' then ' outside quotes when a command verb follows.

    A quote after a letter or digit (e.g. don't) is treated as an apostrophe.
    """
    clauses, start, quote, i = [], 0, "", 0
    while i < len(instruction):
        c = instruction[i]
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"" and not (i and instruction[i - 1].isalnum()):
            quote = c
        elif i and not instruction[i - 1].isspace() and (m := _THEN.match(instruction, i)):
            # only try at the start of a whitespace run: keeps long runs linear
            clauses.append(instruction[start:i])
            start = i = m.end()
            continue
        i += 1
    clauses.append(instruction[start:])
    return clauses


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


_REPLACE_HEAD = _rule(r"replace\s+")
_WITH = _rule(r"(?<=\S)\s++with\s++")
_IN_WORD = _rule(r"(?<=\s)in(?=\s)")
_IN_TAIL = _rule(r".+(?<=\S)\s++in\s++")
_PATH_RE = _rule(_PATH)


class _ReplaceMatcher:
    r"""Linear-time stand-in for the regex
    r"replace\s+(?P<old>.+?)\s+with\s+(?P<new>.+)\s+in\s+" + _PATH,
    which backtracks catastrophically (seconds) on long inputs full of "with"/"in".

    old = text before the first " with "; new/path split at the last " in " when that
    leaves a quote-free path, otherwise at the " in " before a fully quoted final path.
    """

    def fullmatch(self, clause: str) -> dict[str, str] | None:
        head = _REPLACE_HEAD.match(clause)
        if not head:
            return None
        body = clause[head.end():]
        w = _WITH.search(body, 1)  # old must be non-empty
        if not w:
            return None
        old, rest = body[:w.start()], body[w.end():]
        last = None
        for last in _IN_WORD.finditer(rest):
            pass
        if last is not None:
            new, path = rest[:last.start()], rest[last.end():].lstrip()
            if new.strip() and path and _PATH_RE.fullmatch(path):
                return {"old": old, "new": new, "path": path}
        if rest and rest[-1] in "'\"":
            start = rest.rfind(rest[-1], 0, len(rest) - 1)
            if start >= 0 and _IN_TAIL.fullmatch(rest, 0, start) and _PATH_RE.fullmatch(rest, start):
                new = rest[:start].rstrip()[:-2]  # drop the trailing "in"
                return {"old": old, "new": new, "path": rest[start:]}
        return None


_replace = _ReplaceMatcher()

# Separators use possessive \s++ and a (?<=\S) guard so long whitespace runs cannot
# trigger quadratic/cubic backtracking.
_RULES: list[tuple[re.Pattern[str] | _ReplaceMatcher, Callable[..., Action]]] = [
    (_rule(r"(?:read|show|cat|open)\s+" + _ANY_PATH),
     lambda m: Action("read_file", {"path": _unquote(m["path"])})),
    (_rule(r"(?:list|ls)(?:\s+" + _ANY_PATH + ")?"),
     lambda m: Action("list_dir", {"path": _unquote(m["path"] or ".")})),
    (_replace, lambda m: Action("edit_file", {"path": _unquote(m["path"]),
                                              "old": _text(m["old"]), "new": _text(m["new"])})),
    (_rule(r"(?:create|write)\s++" + _LAZY_PATH + r"(?<=\S)\s++with\s++(?P<content>.*)"),
     lambda m: Action("write_file", {"path": _unquote(m["path"]), "content": _text(m["content"])})),
    (_rule(r"write\s++(?P<content>.*)(?<=\S)\s++to\s++" + _PATH),
     lambda m: Action("write_file", {"path": _unquote(m["path"]), "content": _text(m["content"])})),
    (_rule(r"create\s++" + _PATH + r"(?<=\S)\s++with"),  # "create x.txt with" -> empty file
     lambda m: Action("write_file", {"path": _unquote(m["path"]), "content": ""})),
    (_rule(r"(?:run|exec|execute)\s+(?P<command>.+)"),
     lambda m: Action("run_shell", {"command": m["command"].strip()})),
]


_DANGLING_THEN = re.compile(r"^(?:and\s++)?then(?:\s++|$)|(?<=\S)\s++(?:and\s++)?then$", re.IGNORECASE)


def _parse(instruction: str) -> list[Action] | None:
    """Turn an instruction into actions; None if any clause is unrecognised.

    A dangling leading "then " or trailing " then" / " and then" is ignored.
    """
    actions = []
    instruction = _DANGLING_THEN.sub("", instruction.strip()).strip()
    if not instruction:
        return None
    for clause in _split(instruction):
        for pattern, build in _RULES:
            if m := pattern.fullmatch(clause.strip()):
                actions.append(build(m))
                break
        else:
            return None
    return actions or None
