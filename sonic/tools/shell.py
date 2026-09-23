"""Shell tool. [Owner: commit 3]"""
import math
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

from sonic import sandbox
from sonic.errors import ToolError
from sonic.workspace import Workspace

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 24 * 3600
MAX_OUTPUT_CHARS = 20_000
# Bytes kept per stream: enough for MAX_OUTPUT_CHARS of any UTF-8; the rest is counted, not kept.
_CAPTURE_BYTES = MAX_OUTPUT_CHARS * 4
# argv (sandbox profile included) and the environment share ARG_MAX (1 MiB on macOS).
MAX_COMMAND_BYTES = 256 * 1024
MAX_NESTING = 5
# After the shell exits, how long background children may keep the output pipes open.
DRAIN_GRACE = 1.0
# How long to wait for the pipes to close after killing the process group.
KILL_GRACE = 2.0
# sandbox-exec's exit status when it cannot apply the profile.
SANDBOX_START_FAILED = 65

# Every quantifier below is possessive or bounded by a separator/keyword, so a long
# command (100 KB+) is scanned in linear time rather than backtracking for minutes.
_CMD_START = r"(?:^|[;&|(`\n]|\$\()\s*"
# A path prefix (\rm, /bin/rm, ./x/rm); segments can't contain separators.
_PATH = r"\\?(?:[^\s/;&|()`]*/)*+"
# Prefixes that still leave the next word in command position (xargs rm, env rm, \rm, /bin/rm).
_PREFIX = r"(?:(?:sudo|env|command|builtin|exec|nohup|time|nice|xargs(?:\s+-\S+)*+)\s+)*+" + _PATH
_DELETERS = r"(?:rm|rmdir|unlink|shred|srm)"
_WORD_END = r"(?=$|[\s;&|)`])"
_DELETION = "deletion is disabled in Supersonic"
_GIT = r"git(?:\s+-[Cc]\s+\S+|\s+--?[\w-]+(?:=\S+)?)*+\s+"


def _until(word: str, stop: str = r";&|\n") -> str:
    """Characters up to the next separator in `stop` or the next `word` (keeps scans linear)."""
    return rf"(?:(?!\b{word}\b)[^{stop}])*?"


_SHELL_STAGE = re.compile(r"\s*" + _PATH + r"(?:ba|z|da|k|c|tc|fi)?sh\b")
_DECODERS = [(re.compile(r"\bbase64\b"), re.compile(r"\s(?:-d|-D|--decode)\b")),
             (re.compile(r"\bxxd\b"), re.compile(r"\s-r\b"))]


def _decoded_into_shell(view: str) -> bool:
    """`base64 -d ... | sh`, `xxd -r ... | bash`: decoded data piped into a later shell stage."""
    for pipeline in re.split(r"[;&\n]", view):
        stages = pipeline.split("|")
        last_shell = max((i for i, s in enumerate(stages) if _SHELL_STAGE.match(s)), default=-1)
        for stage in stages[:max(last_shell, 0)]:
            for tool, flag in _DECODERS:
                m = tool.search(stage)
                if m and flag.search(stage, m.end()):
                    return True
    return False


_DENYLIST: list[tuple[str, Callable[[str], object]]] = [
    ("sudo", re.compile(_CMD_START + r"sudo\b").search),
    (_DELETION + " (rm of / or home)", re.compile(
        r"\brm\s+(?:-[\w-]+\s+)*+(?:/|~|\$HOME|\$\{HOME\})/?\*?(?=$|[\s;&|)])").search),
    ("mkfs", re.compile(r"\bmkfs\b").search),
    ("dd to a device", re.compile(r"\bdd\b" + _until("dd", ";&|") + r"\bof=/dev/").search),
    ("shutdown/reboot", re.compile(_CMD_START + r"(?:shutdown|reboot|halt|poweroff)\b").search),
    ("fork bomb", re.compile(r"(?<![\w:])([\w:]+)\s*\(\)\s*\{\s*\1\s*\|\s*\1\s*&").search),
    (_DELETION, re.compile(_CMD_START + _PREFIX + _DELETERS + _WORD_END).search),
    (_DELETION + " (find -delete)", re.compile(
        r"\bfind\b" + _until("find") + r"\s-delete\b").search),
    (_DELETION + " (find -exec)", re.compile(
        r"\bfind\b" + _until("find") + r"\s-(?:exec|execdir|ok|okdir)\s+" + _PATH
        + _DELETERS + _WORD_END).search),
    (_DELETION + " (git clean)", re.compile(_CMD_START + _PREFIX + _GIT + "clean" + _WORD_END).search),
    (_DELETION + " (git rm)", re.compile(_CMD_START + _PREFIX + _GIT + "rm" + _WORD_END).search),
    ("decoded data piped into a shell", _decoded_into_shell),
]

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish"}
_INTERPRETER = re.compile(r"(?:python[\d.]*|perl|node|nodejs|ruby|php|deno|bun)")
_INLINE_FLAGS = {"-c", "-e", "-E", "--eval", "-p", "--print", "-r"}
_DELETION_CALLS = re.compile(
    r"\bos\.(?:remove|unlink|rmdir|removedirs)\b|\bshutil\.rmtree\b|\brmtree\b"
    r"|\.(?:unlink|rmdir)\s*\(|\b(?:unlinkSync|rmSync|rmdirSync)\b|\bfs\.(?:rm|unlink|rmdir)\b"
    r"|\bunlink\b|\brmdir\b|\bFileUtils\.rm|\bFile\.delete\b"
    r"|\bfrom\s+os\s+import\b" + _until("from", r"\n;") + r"\b(?:remove|unlink|rmdir|removedirs)\b"
    r"|\bfrom\s+shutil\s+import\b" + _until("from", r"\n;") + r"\brmtree\b")
_TREE_DELETION = re.compile(r"\b(?:rmtree|removedirs|rmSync|rmdirSync|rm_r|rm_rf)\b|\bfs\.rm\b")
_RISKY_TARGET = re.compile(
    r"""["'](?:/|~)|\$HOME|\bexpanduser\b|\bhome\s*\(|\bhomedir\b|\bgetenv\b|\benviron\b|\bENV\b""")

# Separators that are literal text when quoted: `git commit -m "fix; rm later"` runs no rm.
_QUOTED_SEPARATORS = str.maketrans({c: " " for c in ";&|()\n"})
# Constructs whose quoting a simple scan can't follow (substitutions nest quotes, heredoc
# bodies and $'..' have their own rules): commands containing them are scanned unmasked.
_UNMASKABLE = ("$(", "`", "${", "<<", "$'")


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
    shown = command if len(command) <= 500 else command[:500] + "..."
    return ToolError(f"command blocked by denylist ({label}): {shown!r}")


def _tokens(command: str) -> list[str] | None:
    """Shell-like tokens (quotes removed, ;|&() split out), or None if unparseable."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _mask_quoted(command: str) -> str | None:
    """`command` with separators inside '...' and "..." blanked out, quotes kept.

    Returns None when the quoting can't be followed reliably (unterminated quotes or a
    construct in _UNMASKABLE); callers then scan the raw command, which is conservative.
    """
    if any(s in command for s in _UNMASKABLE):
        return None
    out: list[str] = []
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if c == "\\":
            out.append(command[i:i + 2])
            i += 2
        elif c == "#" and (i == 0 or command[i - 1] in " \t\n;&|()"):
            end = command.find("\n", i)
            end = n if end < 0 else end
            out.append(command[i:end])  # comment: left as is (never masked)
            i = end
        elif c in "'\"":
            j = i + 1
            while j < n and command[j] != c:
                j += 2 if c == '"' and command[j] == "\\" else 1
            if j >= n:
                return None
            out.append(c + command[i + 1:j].translate(_QUOTED_SEPARATORS) + c)
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


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
    """Raise ToolError if `command`, or any shell/eval/interpreter payload inside it, is dangerous.

    Patterns see the command with quoted separators blanked (see _mask_quoted) and its
    quote-free token form; payloads of sh -c / eval / python -c are checked recursively.
    """
    if depth > MAX_NESTING:
        raise _blocked("too deeply nested", command)
    tokens = _tokens(command)
    masked = _mask_quoted(command) if tokens is not None else None
    masked_tokens = _tokens(masked) if masked is not None else None
    if masked is not None and masked_tokens is not None:
        views = [masked, " ".join(masked_tokens)]
    else:
        views = [command] if tokens is None else [command, " ".join(tokens)]
    for label, matches in _DENYLIST:
        if any(matches(v) for v in views):
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


def _check_command(command: object) -> str:
    """`command` if it is a runnable, non-blank string; ToolError otherwise."""
    if not isinstance(command, str):
        raise ToolError(f"command must be a string, got {type(command).__name__}")
    if not command.strip():
        raise ToolError("command is empty")
    if "\x00" in command:
        raise ToolError("command contains a NUL byte")
    size = len(command.encode("utf-8", errors="surrogateescape"))
    if size > MAX_COMMAND_BYTES:
        raise ToolError(
            f"command too long ({size} bytes, max {MAX_COMMAND_BYTES}); "
            "write the script to a file and run that instead")
    return command


def _check_timeout(timeout: object) -> float:
    """`timeout` as seconds in (0, MAX_TIMEOUT]; numeric strings (from an LLM) are accepted."""
    if timeout is None:
        return float(DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float, str)):
        raise ToolError(f"timeout must be a number of seconds, got {timeout!r}")
    try:
        seconds = float(timeout.strip() if isinstance(timeout, str) else timeout)
    except (ValueError, OverflowError):
        raise ToolError(f"timeout must be a number of seconds, got {timeout!r}") from None
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_TIMEOUT:
        raise ToolError(f"timeout must be > 0 and <= {MAX_TIMEOUT} seconds, got {timeout!r}")
    return seconds


class _Capture(threading.Thread):
    """Drains one pipe: keeps the first _CAPTURE_BYTES, counts the rest.

    Owns the pipe and closes it when the writer side closes, so a pipe held open by an
    escaped process never has its fd closed (and reused) under a blocked read.
    """

    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream = stream
        self.data = bytearray()
        self.dropped = 0

    def run(self) -> None:
        fd = self.stream.fileno()
        try:
            while chunk := os.read(fd, 65536):
                room = _CAPTURE_BYTES - len(self.data)
                if room > 0:
                    self.data += chunk[:room]
                self.dropped += max(0, len(chunk) - max(room, 0))
        except OSError:
            pass
        finally:
            self.stream.close()

    def text(self) -> str:
        """Captured output, decoded (undecodable bytes replaced) and capped at MAX_OUTPUT_CHARS."""
        text = bytes(self.data).decode("utf-8", errors="replace")
        if len(text) <= MAX_OUTPUT_CHARS and not self.dropped:
            return text
        extra = max(0, len(text) - MAX_OUTPUT_CHARS)
        more = f" chars + {self.dropped} bytes" if self.dropped else " chars"
        return text[:MAX_OUTPUT_CHARS] + f"\n[truncated {extra}{more}]"


def _join(threads: list[_Capture], deadline: float) -> bool:
    """Wait for `threads` until `deadline`; True if all finished."""
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return not any(t.is_alive() for t in threads)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:  # group already gone (ProcessLookupError) or only zombies left (EPERM)
        pass


def run_shell(ws: Workspace, command: str, timeout: float = DEFAULT_TIMEOUT) -> ShellResult:
    """Run `command` via the shell with cwd=ws.root.

    If ws.sandboxed: run via sandbox.wrap(command, ws) with sandbox.env(ws); if
    sandbox.available() is False, raise ToolError telling the user about --no-sandbox.
    Deletion commands (rm, rmdir, unlink, shred, find -delete, git clean, git rm, ...) are
    denylisted too, so the user gets a clear message instead of a sandbox error.
    (`truncate` empties but does not delete a file, so it is allowed.)

    Raises ToolError for denylisted commands (e.g. 'rm -rf /', 'sudo', 'mkfs', fork bombs),
    empty/oversized commands, bad timeouts (numeric strings are accepted), and failures to
    start the shell or sandbox. No other exception escapes.

    stdin is /dev/null, so commands that read it see EOF instead of hanging. Output is
    decoded as UTF-8 with replacement and truncated past MAX_OUTPUT_CHARS; only a bounded
    prefix is kept in memory. The command runs in its own process group, which is killed
    on timeout, and also when background children still hold stdout/stderr DRAIN_GRACE
    seconds after the shell exits (`sleep 30 & echo started` returns promptly).
    Background jobs that redirect their output away are left running.
    """
    command = _check_command(command)
    seconds = _check_timeout(timeout)
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
    try:
        proc = subprocess.Popen(
            args, cwd=ws.root, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except (OSError, ValueError) as e:
        what = f"sandbox ({args[0]})" if ws.sandboxed else "shell"
        raise ToolError(f"could not start {what} in {ws.root}: {e}") from None

    readers = [_Capture(proc.stdout), _Capture(proc.stderr)]
    for r in readers:
        r.start()
    deadline = time.monotonic() + seconds
    timed_out, killed_stragglers = False, False
    try:
        proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc.pid)
    if not timed_out and not _join(readers, min(deadline, time.monotonic() + DRAIN_GRACE)):
        killed_stragglers = True
        _kill_group(proc.pid)
    _join(readers, time.monotonic() + KILL_GRACE)
    try:
        proc.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass
    stdout, stderr = readers[0].text(), readers[1].text()

    if timed_out:
        return ShellResult(None, stdout, stderr, timed_out=True, timeout=seconds)
    if (ws.sandboxed and proc.returncode == SANDBOX_START_FAILED
            and stderr.startswith("sandbox-exec:")):
        raise ToolError(f"sandbox failed to start: {stderr.strip().splitlines()[0]}")
    if killed_stragglers:
        note = "[background processes still holding the output were killed]"
        stderr = f"{stderr}\n{note}" if stderr else note
    return ShellResult(proc.returncode, stdout, stderr)
