#!/usr/bin/env python3
"""Drive a hook-based capture pipeline from a recorded transcript.

    python3 tests/evals/replay.py --repo <checkout> --root <scratch> <fixture.jsonl>

Why this exists: the two detection architectures do not take the same input.
This branch reads a whole transcript at review time, so a fixture is a file
path handed to a slash command. The capture branch never sees a transcript --
it is fed one hook payload at a time *while* a session runs, and its candidates
are whatever its span logic banked along the way. Pointing it at a fixture
would exercise nothing.

So the fixture is replayed as the event stream it would have produced live:
one ``UserPromptSubmit`` per developer message, one ``PostToolUse`` per tool
call with its result attached, a ``Stop`` at the end of each assistant turn,
and a closing ``SessionEnd``. That is the same sequence Claude Code emits, in
the same order, which is what makes a result from either branch comparable.

Two ways the replay is not the live thing, both stated rather than hidden:

- **Time is compressed.** Steps are stamped with the wall clock at replay, so
  a session that took an hour looks instantaneous. Nothing in the span logic
  reads those stamps -- budgets count prompts and steps -- so this changes no
  decision, but a future rule keyed on elapsed time would not be tested here.
- **Nothing is *not* delivered.** A live hook can be skipped: a crashed turn,
  a payload the CLI dropped. The replay always delivers every event, so this
  measures the pipeline's judgement rather than its resilience to gaps.

Deliberately drives the ``skillpp hook`` subprocess rather than importing the
capture module, so one script works against any checkout without either branch
having to import the other.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _events(path: Path, session_id: str, cwd: str) -> list[tuple[str, dict]]:
    """The hook stream a live session would have emitted for this transcript.

    Tool results are matched back to their call by ``tool_use_id`` first, since
    that is what says whether a step failed -- and a failed last step is the
    difference between a span that folds and one that stays pending.
    """
    entries = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    results: dict[str, object] = {}
    for entry in entries:
        content = (entry.get("message") or {}).get("content")
        if entry.get("type") == "user" and isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    results[block.get("tool_use_id", "")] = block.get("content")

    base = {"session_id": session_id, "cwd": cwd,
            "transcript_path": str(path)}
    stream: list[tuple[str, dict]] = []
    for entry in entries:
        message = entry.get("message") or {}
        content = message.get("content")
        if entry.get("type") == "user":
            # A list-shaped user entry is a tool result, not a person typing.
            # Feeding those as prompts would triple the prompt count and trip
            # every span budget on noise the developer never sent.
            if isinstance(content, str) and content.strip():
                stream.append(("UserPromptSubmit", {**base, "prompt": content}))
        elif entry.get("type") == "assistant" and isinstance(content, list):
            calls = [b for b in content if b.get("type") == "tool_use"]
            for block in calls:
                stream.append(("PostToolUse", {
                    **base,
                    "tool_name": block.get("name", ""),
                    "tool_input": block.get("input") or {},
                    "tool_response": results.get(block.get("id", "")),
                }))
            if calls:
                stream.append(("Stop", dict(base)))
    stream.append(("SessionEnd", {**base, "reason": "other"}))
    return stream


def replay(repo: Path, root: Path, path: Path, *,
           session_id: str, verbose: bool = False) -> list[dict]:
    """Feed one transcript through ``skillpp hook`` and report what it said."""
    said = []
    for event, payload in _events(path, session_id, str(repo)):
        proc = subprocess.run(
            [sys.executable, "bin/skillpp", "--root", str(root),
             "hook", "--event", event, "--verbose"],
            cwd=repo, input=json.dumps(payload),
            capture_output=True, text=True)
        out = proc.stdout.strip()
        note = {"event": event, "out": out}
        if event in ("Stop", "SessionEnd", "UserPromptSubmit") and out:
            said.append(note)
        if verbose:
            print(f"  {event:18} {out[:120]}")
    return said


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("transcript", type=Path)
    ap.add_argument("--repo", type=Path, required=True,
                    help="checkout whose bin/skillpp receives the events")
    ap.add_argument("--root", type=Path, required=True, help="scratch ledger")
    ap.add_argument("--session-id", default="replay")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    said = replay(args.repo.resolve(), args.root.resolve(),
                  args.transcript.resolve(),
                  session_id=args.session_id, verbose=args.verbose)
    for note in said:
        print(f"{note['event']}: {note['out']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
