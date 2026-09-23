"""REPL entry point: `python -m sonic [--root DIR]`. [Owner: commit 1]"""
import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sonic", description="Supersonic coding harness")
    parser.add_argument("--root", default=".", help="workspace root (default: cwd)")
    args = parser.parse_args(argv)
    print(f"sonic ready in {args.root}. Type an instruction, or /quit.")
    for line in sys.stdin:
        line = line.strip()
        if line in ("/quit", "/exit"):
            break
        if line:
            print(f"(echo) {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
