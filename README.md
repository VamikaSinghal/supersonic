# Supersonic

## Problem Statement

Build the coding harness of your dreams. That's it.

## Getting Started

### Prerequisites

The sandbox has the following toolchains pre-installed. Use whichever you prefer:

- Python 3.12
- Node.js 20
- Go 1.22
- Java 17 (OpenJDK), with Maven
- .NET 8

### Setup Instructions

Dependencies are installed automatically when you initialize the assessment with the Litmus CLI. Set up whatever project structure and dependencies your harness needs — nothing is pre-scaffolded. You have **75 minutes** of coding time.

## Requirements

The specifics of the harness are entirely up to you, but it must satisfy the following:

1. It must be a runnable program (CLI, TUI, or local web app) that a user can start and interact with.
2. It must accept a task or instruction from the user and take at least one concrete action against a real local filesystem or shell — e.g., reading a file, writing/editing a file, or executing a command — as a result of that instruction. A chat window that only prints text does not satisfy this.
3. This environment has no LLM API keys configured. You do not need a real model call to satisfy Requirement 2 — you may stub, mock, or hardcode the "decision" logic (what to read, what to write, what command to run) however you like. Wire it so that swapping in a real model call later would be straightforward.
4. Everything else — context management, permissions/sandboxing, planning, undo/rollback, streaming output, multi-step execution, multi-agent coordination, UI/UX, logging, whatever you consider essential to a good harness — is entirely your decision to make or skip.

There is no required tech stack, framework, or architecture. Build it the way you'd actually want to use it.

## Submission Guidelines

### What to Submit

- All source code for your harness.
- A 5-minute video demo of what you built. 

The video has a single prompt: **"Explain what you made."** There is no questionnaire and no written report — just show and explain your project on screen.

### How to Submit

```bash
litmus submit
```

After submitting, your browser will direct you to the recording interface where you'll demo your work. 