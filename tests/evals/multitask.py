"""Sessions holding more than one procedure, ported from the other branch.

Where they came from and why they are not written fresh: `feat/episode-filter`
built a benchmark whose ground truth is *how many* procedures a session holds,
and running this branch against it is what exposed the self-match bug. Rewriting
the fixtures here would make the two branches' numbers incomparable for no gain,
so the scripts are transcribed step for step from
`tests/benchmarks/cases.py` on that branch.

**Why these two.** They are the cases where detection was measured as
non-deterministic — four runs each, same input, same code:

    two-tasks-one-sitting:      2, 1, 1, 2
    three-tasks-one-morning:    3, 2, 3, 2

That variance matters more than any single accuracy number, because every score
in either branch comparison was one run. A result reported as 3 of 4 could have
been 2 of 4 on another draw.

**What they deliberately do not contain.** Leading exploration. The proposed
first experiment was to trim it, and neither script has any to trim — one grep
mid-session in `three-tasks`, inside the third task, is not the leading run that
finding was about. Recorded here so nobody spends a run discovering that the
intervention cannot apply.
"""

from __future__ import annotations

import json
from pathlib import Path

from seed_recurrence import build


def _script(out: Path, name: str, steps: list[tuple]) -> Path:
    """Turn a step script into a transcript.

    A `prompt` step is a developer turn; everything else is a tool call with a
    result, so a reader sees the same `>` / `$` / `=` shape as any real session.
    """
    user, call, result, says = build([0])
    records: list[dict] = []
    for tool, payload, failed in steps:
        if tool == "prompt":
            records.append(user(payload))
            continue
        if isinstance(payload, dict):
            records.append(call(tool, **payload))
        elif tool == "Bash":
            records.append(call(tool, command=payload))
        else:
            records.append(call(tool, file_path=payload))
        records.append(result("failed" if failed else "ok", error=failed))
    path = out / f"{name}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records),
                    encoding="utf-8")
    return path


def two_tasks(out: Path) -> Path:
    """A release and an unrelated CI bump. Two procedures, one sitting."""
    return _script(out, "two-tasks-one-sitting", [
        ("prompt", "cut the 2.4 release tag", False),
        ("Bash", "npm test", False),
        ("Bash", "git tag -s v2.4.0 -m rel", False),
        ("Bash", "git push --follow-tags", False),
        ("prompt", "now bump CI to node 22", False),
        ("Edit", ".github/workflows/ci.yml", False),
        ("Bash", "npm test", False),
        ("Bash", "git commit -am 'ci: node 22'", False),
        ("Bash", "git push", False),
    ])


def three_tasks(out: Path) -> Path:
    """A release, a dependency bump and a hotfix. Three procedures."""
    return _script(out, "three-tasks-one-morning", [
        ("prompt", "cut the 2.4 release tag", False),
        ("Bash", "npm test", False),
        ("Bash", "git tag -s v2.4.0 -m rel", False),
        ("Bash", "git push --follow-tags", False),
        ("prompt", "now bump lodash to 4.17.21", False),
        ("Edit", "package.json", False),
        ("Bash", "npm ci", False),
        ("Bash", "npm test", False),
        ("Bash", "git commit -am 'chore: bump lodash'", False),
        ("prompt", "and the login redirect is broken on staging", False),
        ("Bash", "grep -rn redirect src/auth/", False),
        ("Edit", "src/auth/login.py", False),
        ("Bash", "pytest tests/test_auth.py", False),
        ("Bash", "git commit -am 'fix: login redirect'", False),
    ])


# name -> (builder, how many distinct procedures a correct run records)
ALL = {
    "two-tasks-one-sitting": (two_tasks, 2),
    "three-tasks-one-morning": (three_tasks, 3),
}
