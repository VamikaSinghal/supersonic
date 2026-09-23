# Supersonic

A local coding harness you can trust with your machine: it plans, asks before it changes anything, shows you a diff, **can't delete your files**, and can undo every change it made.

```bash
uv venv -p 3.12 .venv && uv pip install --python .venv/bin/python pytest   # one-time
.venv/bin/python -m sonic --root ./playground          # terminal REPL
.venv/bin/python -m sonic --root ./playground --web    # local web UI → http://127.0.0.1:8765
```

Standard library only (Python 3.12). `pytest` is the only dev dependency.

## Try it

```
sonic> create hello.py with print('hi') then run python3 hello.py
write_file hello.py (11 bytes)
  allow? [y/n/a] y
✓ write_file → wrote 11 bytes to hello.py
run_shell: python3 hello.py
  allow? [y/n/a] y
✓ run_shell → exit 0
    --- stdout ---
    hi
completed 2 step(s)
sonic> replace hi with hello in hello.py
sonic> /undo
undid edit_file hello.py (replaced version moved to .sonic/trash/1/hello.py)
sonic> run rm hello.py
✗ run_shell → error: command blocked by denylist (deletion is disabled in Supersonic)
```

| Instruction (chain with `then`) | Tool |
|---|---|
| `read <path>` · `list [<path>]` | read_file · list_dir (no approval needed) |
| `create <path> with <content>` · `write <content> to <path>` | write_file |
| `replace <old> with <new> in <path>` | edit_file (must match exactly once) |
| `run <command>` | run_shell (sandboxed) |

Paths may contain spaces; quotes keep a literal ` then ` inside text.
Slash commands: `/undo`, `/log`, `/help`, `/quit`.

Flags: `--yolo` (skip approvals) · `--no-sandbox` · `--max-steps N` · `--planner stub|llm` · `--web [--port N]`.

## How it works

```
 instruction ─▶ Planner ──Action──▶ Agent loop ──approve?──▶ Tool ──▶ Step(ok, observation)
                  ▲                    │   └─ UndoStack snapshot (write/edit)       │
                  └──── history ◀──────┴──────────── SessionLog (.sonic/sessions) ◀─┘
```

- **`sonic/planner.py`**: the `Planner` protocol (`next_action(instruction, history) -> Action | Done`) and `StubPlanner`, a rule-based stand-in for a model.
- **`sonic/llm_planner.py`**: `LLMPlanner`, the same protocol backed by the Anthropic Messages API with tool use. Run it with `--planner llm` and `ANTHROPIC_API_KEY`. The HTTP client is injected, so tests use a fake. Swapping planners changes nothing else.
- **`sonic/agent.py`**: the loop: plan → validate args → approve → snapshot → execute → observe, capped at `max_steps`. Tool errors, denials and non-zero exits become failed steps, never crashes.
- **`sonic/tools/`**: `fs.py` (read/list/write/edit, sandboxed to the workspace) and `shell.py` (timeouts, output truncation, denylist).
- **`sonic/undo.py`**: undo stack. Undo *moves* the current version to `.sonic/trash/<n>/` and restores the previous one. Nothing is destroyed.
- **`sonic/log.py`**: one JSONL file per session in `.sonic/sessions/`.
- **`sonic/web.py` + `sonic/static/index.html`**: the local web UI (timeline, diff-before-approve, undo button). It binds to 127.0.0.1 only, POSTs need a per-session token, and requests with a foreign `Host` header are refused.

## Safety model: no deletion, anywhere

| Layer | What it does |
|---|---|
| **Kernel sandbox** (`sonic/sandbox.py`) | Every shell command runs under macOS `sandbox-exec`. Deleting, renaming over, or unlinking any file is denied by the OS, including via `eval`, base64, or `python -c`. Writes outside the workspace are denied. Only a private scratch `$TMPDIR` allows deletion. |
| **Fail closed** | No sandbox available (e.g. Linux) → shell commands are refused unless you pass `--no-sandbox`. |
| **Denylist** | `rm`, `rmdir`, `unlink`, `shred`, `find -delete`, `git clean`, `sudo`, `mkfs`, fork bombs, also inside `bash -c` / `eval` / decoded pipes, are blocked with a clear message before anything runs. |
| **Workspace jail** | File tools resolve symlinks and reject `../`, absolute paths, and escapes. There is no delete tool. |
| **Approvals** | Writes, edits and shell commands need `y` / `n` / `a` (always). Reads don't. |
| **Undo** | Every write/edit can be undone; replaced versions go to `.sonic/trash`. |

Known trade-off: because rename-over counts as deletion, tools that save by writing a temp file and renaming it (git, some editors, package managers) can't write inside the sandboxed workspace. Run them outside the harness, or use `--no-sandbox` deliberately.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Every test runs with a fake `HOME`. Denylist tests replace `subprocess` with a guard, so a regression fails the test instead of running the command. Sandbox tests only target throwaway canary files.

## Original brief

### Problem Statement

Build the coding harness of your dreams. That's it.

### Getting Started

### Prerequisites

The sandbox has the following toolchains pre-installed. Use whichever you prefer:

- Python 3.12
- Node.js 20
- Go 1.22
- Java 17 (OpenJDK), with Maven
- .NET 8

### Setup Instructions

Dependencies are installed automatically when you initialize the assessment with the Litmus CLI. Set up whatever project structure and dependencies your harness needs — nothing is pre-scaffolded. You have **75 minutes** of coding time.

### Requirements

The specifics of the harness are entirely up to you, but it must satisfy the following:

1. It must be a runnable program (CLI, TUI, or local web app) that a user can start and interact with.
2. It must accept a task or instruction from the user and take at least one concrete action against a real local filesystem or shell — e.g., reading a file, writing/editing a file, or executing a command — as a result of that instruction. A chat window that only prints text does not satisfy this.
3. This environment has no LLM API keys configured. You do not need a real model call to satisfy Requirement 2 — you may stub, mock, or hardcode the "decision" logic (what to read, what to write, what command to run) however you like. Wire it so that swapping in a real model call later would be straightforward.
4. Everything else — context management, permissions/sandboxing, planning, undo/rollback, streaming output, multi-step execution, multi-agent coordination, UI/UX, logging, whatever you consider essential to a good harness — is entirely your decision to make or skip.

There is no required tech stack, framework, or architecture. Build it the way you'd actually want to use it.

### Submission Guidelines

### What to Submit

- All source code for your harness.
- A 5-minute video demo of what you built. 

The video has a single prompt: **"Explain what you made."** There is no questionnaire and no written report — just show and explain your project on screen.

### How to Submit

```bash
litmus submit
```

After submitting, your browser will direct you to the recording interface where you'll demo your work. 