"""Shell tool. [Owner: commit 3]"""
import os
import re
import shlex
import signal
import subprocess
from dataclasses import dataclass

from sonic import sandbox
from sonic.errors import ToolError
from sonic.workspace import Workspace

DEFAULT_TIMEOUT = 30
MAX_OUTPUT_CHARS = 20_000
MAX_NESTING = 5

_CMD_START = r"(?:^|[;&|(`\n]|\$\()\s*"
# Prefixes that still leave the next word in command position (xargs rm, env rm, \rm, /bin/rm).
_PREFIX = r"(?:(?:sudo|env|command|builtin|exec|nohup|time|nice|xargs(?:\s+-\S+)*)\s+)*\\?(?:\S*/)?"
_DELETERS = r"(?:rm|rmdir|unlink|shred|srm)"
_WORD_END = r"(?=$|[\s;&|)`])"
_DELETION = "deletion is disabled in Supersonic"

_DENYLIST: list[tuple[str, re.Pattern[str]]] = [
    ("sudo", re.compile(_CMD_START + r"sudo\b")),
    (_DELETION + " (rm of / or home)", re.compile(
        r"\brm\s+(?:-{1,2}[\w-]+\s+)*(?:/|~|\$HOME|\$\{HOME\})/?\*?(?=$|[\s;&|)])")),
    ("mkfs", re.compile(r"\bmkfs\b")),
    ("dd to a device", re.compile(r"\bdd\b[^;&|]*\bof=/dev/")),
    ("shutdown/reboot", re.compile(_CMD_START + r"(?:shutdown|reboot|halt|poweroff)\b")),
    ("fork bomb", re.compile(r"([\w:]+)\s*\(\)\s*\{\s*\1\s*\|\s*\1\s*&")),
    (_DELETION, re.compile(_CMD_START + _PREFIX + _DELETERS + _WORD_END)),
    (_DELETION + " (find -delete)", re.compile(r"\bfind\b[^;&|\n]*\s-delete\b")),
    (_DELETION + " (find -exec)", re.compile(
        r"\bfind\b[^;&|\n]*\s-(?:exec|execdir|ok|okdir)\s+\\?(?:\S*/)?" + _DELETERS + _WORD_END)),
    (_DELETION + " (git clean)", re.compile(
        _CMD_START + r"git(?:\s+-[Cc]\s+\S+|\s+--?[\w-]+(?:=\S+)?)*\s+clean\b")),
    ("decoded data piped into a shell", re.compile(
        r"(?:\bbase64\b[^|;&\n]*\s(?:-d|-D|--decode)\b|\bxxd\b[^|;&\n]*\s-r\b)"
        r"[^;&\n]*\|\s*(?:\S*/)?(?:ba|z|da|k|c|tc|fi)?sh\b")),
]

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish"}
_INTERPRETER = re.compile(r"(?:python[\d.]*|perl|node|nodejs|ruby|php|deno|bun)")
_INLINE_FLAGS = {"-c", "-e", "-E", "--eval", "-p", "--print", "-r"}
_DELETION_CALLS = re.compile(
    r"\bos\.(?:remove|unlink|rmdir|removedirs)\b|\bshutil\.rmtree\b|\brmtree\b"
    r"|\.(?:unlink|rmdir)\s*\(|\b(?:unlinkSync|rmSync|rmdirSync)\b|\bfs\.(?:rm|unlink|rmdir)\b"
    r"|\bunlink\b|\brmdir\b|\bFileUtils\.rm|\bFile\.delete\b"
    r"|\bfrom\s+os\s+import\b[^\n;]*\b(?:remove|unlink|rmdir|removedirs)\b"
    r"|\bfrom\s+shutil\s+import\b[^\n;]*\brmtree\b")
_TREE_DELETION = re.compile(r"\b(?:rmtree|removedirs|rmSync|rmdirSync|rm_r|rm_rf)\b|\bfs\.rm\b")
_RISKY_TARGET = re.compile(
    r"""["'](?:/|~)|\$HOME|\bexpanduser\b|\bhome\s*\(|\bhomedir\b|\bgetenv\b|\benviron\b|\bENV\b""")


@dataclass
class ShellResult:
    exit_code: int | None  # None if timed out
    stdout: str
    stderr: str
    timed_out: bool = False
    timeout: float | None = None

    def __str__(self) -> str:
        """Observation text for the planner: exit code (or 'timed out'), stdout, stderr."""
        if self.timed_out:
            parts = [f"timed out after {self.timeout}s" if self.timeout is not None else "timed out"]
        else:
            parts = [f"exit {self.exit_code}"]
        if self.stdout:
            parts.append(f"--- stdout ---\n{self.stdout}")
        if self.stderr:
            parts.append(f"--- stderr ---\n{self.stderr}")
        return "\n".join(parts)


def _blocked(label: str, command: str) -> ToolError:
    return ToolError(f"command blocked by denylist ({label}): {command!r}")


def _tokens(command: str) -> list[str] | None:
    """Shell-like tokens (quotes removed, ;|&() split out), or None if unparseable."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _inline_payload(tokens: list[str], i: int, is_shell: bool) -> str | None:
    """The code argument after tokens[i]'s inline-code flag (sh -c / -lc, python -c, node -e)."""
    j = i + 1
    while j < len(tokens) and tokens[j].startswith("-"):
        opt = tokens[j]
        hit = ("c" in opt and not opt.startswith("--")) if is_shell else opt in _INLINE_FLAGS
        if hit:
            return tokens[j + 1] if j + 1 < len(tokens) else None
        j += 1
    return None


def _check_denylist(command: str, depth: int = 0) -> None:
    """Raise ToolError if `command`, or any shell/eval/interpreter payload inside it, is dangerous."""
    if depth > MAX_NESTING:
        raise _blocked("too deeply nested", command)
    tokens = _tokens(command)
    views = [command] if tokens is None else [command, " ".join(tokens)]
    for label, pattern in _DENYLIST:
        if any(pattern.search(v) for v in views):
            raise _blocked(label, command)
    for i, tok in enumerate(tokens or []):
        name = os.path.basename(tok)
        if name in _SHELLS:
            payload = _inline_payload(tokens, i, True)
            if payload is not None:
                _check_denylist(payload, depth + 1)
        elif name == "eval":
            rest = []
            for t in tokens[i + 1:]:
                if t in {";", "&", "&&", "|", "||", "(", ")"}:
                    break
                rest.append(t)
            _check_denylist(" ".join(rest), depth + 1)
        elif _INTERPRETER.fullmatch(name):
            payload = _inline_payload(tokens, i, False)
            if payload is not None and (_TREE_DELETION.search(payload) or (
                    _DELETION_CALLS.search(payload) and _RISKY_TARGET.search(payload))):
                raise _blocked(_DELETION + f" ({name} deletion call)", command)


def _truncate(text: str) -> str:
    """Cap `text` at MAX_OUTPUT_CHARS with a marker."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n[truncated {len(text) - MAX_OUTPUT_CHARS} chars]"


def _as_text(data: str | bytes | None) -> str:
    if data is None:
        return ""
    return data.decode(errors="replace") if isinstance(data, bytes) else data


def run_shell(ws: Workspace, command: str, timeout: float = DEFAULT_TIMEOUT) -> ShellResult:
    """Run `command` via the shell with cwd=ws.root.

    If ws.sandboxed: run via sandbox.wrap(command, ws) with sandbox.env(ws); if
    sandbox.available() is False, raise ToolError telling the user about --no-sandbox.
    Deletion commands (rm, rmdir, unlink, shred, find -delete, git clean, ...) are
    denylisted too, so the user gets a clear message instead of a sandbox error.

    Raises ToolError for denylisted commands (e.g. 'rm -rf /', 'sudo', 'mkfs', fork bombs).
    Kills the process on timeout. Truncates stdout/stderr past MAX_OUTPUT_CHARS.
    """
    _check_denylist(command)
    if ws.sandboxed:
        if not sandbox.available():
            raise ToolError(
                "shell sandbox unavailable on this platform (needs macOS sandbox-exec); "
                "rerun with --no-sandbox to run commands unsandboxed"
            )
        args = sandbox.wrap(command, ws)
        env: dict[str, str] | None = sandbox.env(ws)
    else:
        args, env = ["/bin/sh", "-c", command], None
    proc = subprocess.Popen(
        args, cwd=ws.root, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        return ShellResult(
            None, _truncate(_as_text(stdout)), _truncate(_as_text(stderr)),
            timed_out=True, timeout=timeout,
        )
    return ShellResult(proc.returncode, _truncate(stdout), _truncate(stderr))
