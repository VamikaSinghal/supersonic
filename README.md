# Supersonic

A local coding harness you can trust with your machine. It plans, asks before it changes anything, shows you a diff, **can't delete your files**, undoes its own changes, and keeps a second brain of your project.

```bash
uv venv -p 3.12 .venv && uv pip install --python .venv/bin/python pytest   # one-time setup
.venv/bin/python -m sonic --root ./playground          # terminal REPL
.venv/bin/python -m sonic --root ./playground --web    # web UI → http://127.0.0.1:8765
```

Python 3.12, standard library only. `pytest` is the only dev dependency.

## Features

| | |
|---|---|
| **Agent loop** | plan → check args → approve → snapshot → execute → observe, repeated up to `--max-steps`. Tool errors, denials and non-zero exits become failed steps; they never crash the loop. |
| **Tools** | `read_file`, `list_dir`, `write_file`, `edit_file` (the old text must match exactly once), `run_shell` (timeout, output truncation). |
| **Swappable planner** | `StubPlanner` (rule-based, the default) and `LLMPlanner` (Anthropic tool use, `--planner llm` + `ANTHROPIC_API_KEY`) share one `Planner` protocol. Nothing else changes when you swap them. |
| **Approvals** | Reads run straight away. Writes, edits and shell commands ask `y` / `n` / `a` (always). `--yolo` skips asking. |
| **No-deletion safety** | See [Safety](#safety). |
| **Undo** | `/undo` (or the web Undo button) reverts the last write or edit. The replaced version is moved to `.sonic/trash/`, so nothing is ever destroyed. |
| **Second brain** | A context graph in `.sonic/context/graph.json` (details below). |
| **Session log** | One JSONL file per session in `.sonic/sessions/`. `/log` shows the recent steps. |
| **Web UI** | Light theme in the style of Claude. Chat-style timeline, a diff with line numbers before each approval (Enter to allow, Esc to deny), undo, and a **Brain** tab: an interactive graph with search, remember, forget, and filters. Works on phone-width screens. |
| **REPL** | `/undo` · `/log` · `/remember <fact>` · `/context <query>` · `/brain` · `/help` · `/quit` |

**Stub planner grammar** (chain steps with `then`): `read <path>` · `list [<path>]` · `create <path> with <text>` · `write <text> to <path>` · `replace <old> with <new> in <path>` · `run <command>`. Paths may contain spaces, and quotes protect a literal ` then `.

### Second brain

- **Captured automatically:** each instruction becomes a task node, linked to the files it read, wrote or edited and the commands it ran. Python files are parsed, so functions, classes and imports become nodes and links.
- **Your notes:** `/remember auth.py uses JWT #security` stores a note linked to `auth.py` and `#security`.
- **Recall:** your notes rank first, then connected files, code and recent tasks. The LLM planner gets this in its system prompt, and `/context <query>` shows you exactly what it sees.
- **Forget** archives a node and never erases it. Everything stays on your machine.

### Safety

| Layer | What it does |
|---|---|
| **Kernel sandbox** | Shell commands run under macOS `sandbox-exec`. The OS denies deleting, unlinking, or renaming over any file, even via `eval`, base64, or `python -c`. Writes outside the workspace are denied. |
| **Fail closed** | No sandbox available → shell commands are refused unless you pass `--no-sandbox`. |
| **Denylist** | `rm`, `rmdir`, `unlink`, `shred`, `find -delete`, `git clean`, `sudo`, `mkfs` and fork bombs are blocked with a clear message, including inside `bash -c`, `eval`, and decoded pipes. |
| **Workspace jail** | File tools reject `../`, absolute paths, and symlink escapes. There is no delete tool. The harness's own `.sonic/` folder (graph, trash, logs) is read-only to the agent, for file tools and shell alike. |
| **Web** | Binds to 127.0.0.1 only, POSTs need a per-session token, and foreign `Host` headers are refused. |

## Tests

```bash
.venv/bin/python -m pytest -q
```

403 tests, written before each feature, plus four edge-case rounds (file tools, shell/sandbox, agent/planners, graph/CLI) that found and fixed about 50 bugs. Safety rules for the suite itself:
- `HOME` is faked for every test.
- Denylist tests replace `subprocess` with a guard, so a regression fails the test instead of running the command.
- Sandbox tests only target throwaway canary files.

## Assumptions

- **Local and single-user:** you run it on your own machine, on your own code.
- **macOS for the full sandbox:** other platforms fail closed unless you pass `--no-sandbox`.
- **No API key by default:** the rule-based stub planner stands in for the model. The LLM planner is fully wired but was only tested against a fake client.
- **The workspace is the boundary:** the harness may touch only `--root` (default: the current directory).
- **Deleting a file is never acceptable,** so undo moves files to trash and "forget" archives.

## Trade-offs

- **Strict no-deletion breaks some tools.** Git, some editors and package managers save by writing a temp file and renaming it over the original, which the sandbox counts as a delete. Inside the harness, they fail.
- **Shell changes can't be undone.** `/undo` covers the harness's own file tools only. The sandbox prevents deletion, but a shell command can still overwrite a workspace file.
- **The denylist is best-effort.** It only reads command text and can have false positives. The kernel sandbox is the real guarantee.
- **Stdlib only.** Zero install friction, at the cost of hand-rolled pieces: web polling instead of WebSockets, and a canvas graph without a library.
- **Retrieval is keyword + graph, not embeddings.** It's explainable and offline, but less semantic.
- **Last writer wins on the graph file** if two harness processes share one workspace.
- **The stub grammar is narrow.** Unquoted paths can't contain `with`, `in`, or `to`.

## With more time

1. **Multi-agent coordination:** a lead planner that splits a task across workers in isolated workspaces, with file ownership, a shared context graph, review-then-merge, and one approval lane per agent in the web UI.
2. **Undo for shell changes:** a copy-on-write snapshot of the workspace before each shell step.
3. **Linux sandbox** (Landlock or bubblewrap), plus an opt-in "git-safe" profile that allows rename-over inside `.git/`.
4. **Real LLM runs:** streaming output, prompt caching, token budgets, and an eval set of coding tasks.
5. **Smarter memory:** embeddings alongside keyword retrieval, automatic file summaries, un-archive, and session resume from the graph.
6. **Web hardening:** edge-case tests for the web API and live streaming of shell output.

<details>
<summary>Original assessment brief</summary>

### Problem Statement

Build the coding harness of your dreams. That's it.

### Getting Started

#### Prerequisites

The sandbox has the following toolchains pre-installed. Use whichever you prefer:

- Python 3.12
- Node.js 20
- Go 1.22
- Java 17 (OpenJDK), with Maven
- .NET 8

#### Setup Instructions

Dependencies are installed automatically when you initialize the assessment with the Litmus CLI. Set up whatever project structure and dependencies your harness needs — nothing is pre-scaffolded. You have **75 minutes** of coding time.

### Requirements

The specifics of the harness are entirely up to you, but it must satisfy the following:

1. It must be a runnable program (CLI, TUI, or local web app) that a user can start and interact with.
2. It must accept a task or instruction from the user and take at least one concrete action against a real local filesystem or shell — e.g., reading a file, writing/editing a file, or executing a command — as a result of that instruction. A chat window that only prints text does not satisfy this.
3. This environment has no LLM API keys configured. You do not need a real model call to satisfy Requirement 2 — you may stub, mock, or hardcode the "decision" logic (what to read, what to write, what command to run) however you like. Wire it so that swapping in a real model call later would be straightforward.
4. Everything else — context management, permissions/sandboxing, planning, undo/rollback, streaming output, multi-step execution, multi-agent coordination, UI/UX, logging, whatever you consider essential to a good harness — is entirely your decision to make or skip.

There is no required tech stack, framework, or architecture. Build it the way you'd actually want to use it.

### Submission Guidelines

#### What to Submit

- All source code for your harness.
- A 5-minute video demo of what you built. 

The video has a single prompt: **"Explain what you made."** There is no questionnaire and no written report — just show and explain your project on screen.

#### How to Submit

```bash
litmus submit
```

After submitting, your browser will direct you to the recording interface where you'll demo your work.
</details>
