"""A session of ordinary work, worth no skill at all.

The detector's most common correct answer is "nothing here", and it is the
answer nothing else tests. A prompt tuned only against sessions that *do*
contain a procedure drifts toward finding one everywhere, and a store full of
`running-the-test-suite` is a store nobody opens.

Long enough to clear the review floor, so a refusal here is judgement rather
than the floor declining on the model's behalf.
"""

from __future__ import annotations

import json
from pathlib import Path

from seed_recurrence import build  # noqa: E402  (path fixed up by the runner)


def transcript(out: Path) -> Path:
    user, call, result, says = build([0])
    records = [
        user("what's on this branch"),
        call("Bash", command="git status"),
        result("On branch feat/x\nnothing to commit, working tree clean"),
        says("Clean. Nothing staged."),
        user("merge main in"),
        call("Bash", command="git fetch origin"),
        result("From github.com:acme/app\n   a1b2c3d..e4f5g6h  main -> origin/main"),
        call("Bash", command="git merge origin/main"),
        result("Updating a1b2c3d..e4f5g6h\nFast-forward\n 3 files changed"),
        says("Fast-forwarded, no conflicts."),
        user("run the tests"),
        call("Bash", command="python3 -m pytest -q"),
        result("48 passed in 2.1s"),
        says("All green."),
        user("what changed in those 3 files"),
        call("Bash", command="git diff --stat HEAD~1"),
        result(" README.md | 4 +-\n setup.cfg | 2 +-\n src/app.py | 11 ++++--"),
        says("Docs, a config bump, and a small change in app.py."),
        user("show me the app.py bit"),
        call("Read", file_path="src/app.py"),
        result("def main():\n    ...\n    return 0"),
        says("A guard added around the entry point."),
        user("fine, push it"),
        call("Bash", command="git push origin feat/x"),
        result("To github.com:acme/app\n   a1b2c3d..e4f5g6h  feat/x -> feat/x"),
        says("Pushed."),
        user("thanks"),
        says("Anytime."),
    ]
    path = out / "mundane.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path
