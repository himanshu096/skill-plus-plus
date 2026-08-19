#!/usr/bin/env python3
"""Run the eval fixtures through the capture branch's detection. No model calls.

    python3 tests/evals/bakeoff.py --repo <checkout of the capture branch>
    SKILLPP_MAX_SPAN_PROMPTS=4 python3 tests/evals/bakeoff.py --repo ...

Why it is free: on that branch detection is code, so what it would have
captured from a session can be measured exactly, with no model in the loop.
Only its *review* half needs one. That asymmetry is worth stating plainly --
this branch cannot answer "was there a procedure here" without a frontier call,
and that one can.

What is measured is recall and precision of candidates against the same
fixtures `run.py` uses, whose ground truth is written down in each case's
`asks`. Findings live in `docs/bakeoff.md`; this is the instrument.

Read the two caveats in that document before quoting a number from here. The
fixtures were written for this branch's detector, so a miss can be a fixture
bias as easily as a design limit, and the control experiments are what tell
them apart.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "tests" / "evals"),
                str(REPO / "tests" / "fixtures")]

from replay import replay  # noqa: E402


def fixtures(out: Path) -> dict[str, Path]:
    import mundane
    import sessions
    return {
        "new/match(a)": REPO / "tests/fixtures/recurrence-a.jsonl",
        "match(b)": REPO / "tests/fixtures/recurrence-b.jsonl",
        "big": sessions.big(out),
        "incomplete": sessions.incomplete(out),
        "two": sessions.two(out),
        "secrets": sessions.secrets(out),
        "barren": mundane.transcript(out),
    }


def banked(theirs: Path, root: Path) -> list[dict]:
    out = subprocess.run([sys.executable, "bin/skillpp", "--root", str(root),
                          "review", "--all", "--json"],
                         cwd=theirs, capture_output=True, text=True)
    text = out.stdout.strip()
    return json.loads(text) if text.startswith("[") else []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", type=Path, required=True,
                    help="checkout of the branch whose capture is measured")
    ap.add_argument("--case", action="append", help="run only these")
    ap.add_argument("--keep", type=Path, help="write scratch ledgers here")
    args = ap.parse_args()

    theirs = args.repo.resolve()
    scratch = args.keep or Path(tempfile.mkdtemp(prefix="skillpp-bakeoff-"))
    scratch.mkdir(parents=True, exist_ok=True)
    generated = scratch / "generated"
    generated.mkdir(exist_ok=True)

    for label, path in fixtures(generated).items():
        if args.case and label not in args.case:
            continue
        root = scratch / f"ledger-{label.replace('/', '-')}"
        shutil.rmtree(root, ignore_errors=True)
        replay(theirs, root, Path(path), session_id=label)
        entries = banked(theirs, root)
        log = root / "skillpp.log"
        dropped = log.read_text().splitlines() if log.exists() else []
        kb = Path(path).stat().st_size // 1024
        print(f"\n=== {label}  ({kb} KB) — banked {len(entries)} ===")
        for entry in entries:
            print(f"    x{entry['occurrences']}  {entry['steps']:4} steps  "
                  f"{entry['title'][:60]!r}")
        for line in dropped:
            print(f"    {line.split(' span ')[-1]}")

    if not args.keep:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
