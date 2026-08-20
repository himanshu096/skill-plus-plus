#!/usr/bin/env python3
"""Can a local model decide which sessions are worth a frontier call?

    python3 tests/evals/triage.py                    # whole session
    python3 tests/evals/triage.py --aspect prompts   # the requests alone
    python3 tests/evals/triage.py --model granite3.3:8b

Scored against `truth.py` — sessions the pipeline has already reviewed, labelled
by what the frontier judge actually proposed. Real sessions, real labels, no
fixtures.

Why this and not the span pass: of the last ten sessions drained, five stopped
free at the message floor and five spent a frontier call to find nothing. A
session-level decision would have saved all five. The span pass answered a
harder question correctly and reduced nothing.

**Recall is the metric, not accuracy.** A session wrongly sent on costs one
reading, which is what happens today anyway. A session wrongly dismissed is
never looked at again. So a false negative is the expensive error and the score
below reports them separately rather than folding both into one number.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "tests" / "evals")]

from skillpp.local import PROMPTS, _ask, _yes_no, worth_reading  # noqa: E402
from skillpp.transcript import read                # noqa: E402
from skillpp.window import keep_aspects, segments  # noqa: E402
from truth import labelled                         # noqa: E402


def render(transcript: Path, aspects: tuple[str, ...]) -> str:
    segs = segments(read(transcript))
    if not aspects:
        return "\n".join(s.text for s in segs)
    return "\n".join(keep_aspects(s.text, aspects) for s in segs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument("--aspect", action="append",
                    help="withhold the rest of the session; repeatable")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    aspects = tuple(args.aspect or ())
    template = (PROMPTS / "triage.md").read_text(encoding="utf-8")
    rows = labelled()
    if not rows:
        print("No reviewed sessions with a surviving transcript to score on.")
        return 1

    missed, wasted, right, spent, free = [], [], 0, 0.0, 0
    label = f" [{', '.join(aspects)} only]" if aspects else " [whole session]"
    print(f"=== {args.model}{label} ===\n")
    for session in rows:
        text = render(session.transcript, aspects)
        if not worth_reading(text):
            # Decided in code. Counted apart from the model's answers, because
            # folding these in flatters the score without discriminating.
            free += 1
            want = session.has_procedure
            right += not want
            if want:
                missed.append(session.session[:8])
            print(f"  {session.session[:8]}  want {'yes' if want else 'no ':<3}  "
                  f"{'skipped free — no request to read':<38} "
                  f"{'ok' if not want else 'MISSED'}")
            continue
        prompt = template.replace("{SESSION}", text)
        start = time.monotonic()
        reply = _ask(args.model, prompt)
        took = time.monotonic() - start
        spent += took
        said = _yes_no(reply)
        want = session.has_procedure
        ok = said is want
        right += ok
        if said is not True and want:
            missed.append(session.session[:8])
        if said is True and not want:
            wasted.append(session.session[:8])
        mark = "ok" if ok else ("MISSED" if want else "wasted a call")
        print(f"  {session.session[:8]}  want {'yes' if want else 'no ':<3}  "
              f"said {str(said):<5}  {len(prompt)//4:>6} tok  "
              f"{took:>5.1f}s  {mark}")
        if args.show:
            print(f"        {reply.strip()[:100]!r}")

    total = len(rows)
    with_proc = sum(1 for s in rows if s.has_procedure)
    asked = total - free
    print(f"\n  {right}/{total} correct in {spent:.0f}s"
          f"   ({free} decided free, {asked} asked)")
    print(f"  missed a real procedure: {len(missed)}/{with_proc}"
          + (f"  {missed}" if missed else "")
          + "   <- the expensive error")
    print(f"  wasted a frontier call:  {len(wasted)}/{total - with_proc}"
          + (f"  {wasted}" if wasted else ""))
    saved = (total - with_proc) - len(wasted)
    print(f"  calls saved:             {saved}/{total - with_proc} of the "
          f"empty sessions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
