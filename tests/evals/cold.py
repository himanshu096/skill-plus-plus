"""Fixtures the locator prompt has never been tuned against.

`tests/evals/locator.py` holds fifteen labelled segments and the prompt was
revised three times against them, each revision fixing a rule that had produced
one wrong answer. A clean sweep of that set therefore shows mostly that the
prompt fits it. These exist so the question can be asked cold.

Written and labelled before any of them was run, and the labels are not to be
revised because an answer disagrees. If one of these fails, the prompt is wrong
or the fixture is wrong -- and deciding which has to be argued from the fixture's
own notes, out loud, not settled by editing the label.

Each shape here is one the tuned set does not contain:

- **`abandoned`** — attempted, failed, reverted. The tuned set's only failure is
  in `retry`, where the second attempt succeeds, so "a failure inside finished
  work" is covered and "a failure that ended it" is not.
- **`bespoke`** — finished through a project's own script, with no commit, no
  test runner and no deployment verb anywhere. This is the case that branch's
  `docs/detection.md` says its regex discarded, and it asks whether the model
  reasons about what a command did or matches a vocabulary.
- **`mutation_failed`** — the mutation itself is refused. `npm publish` is in
  that branch's closing vocabulary, so a regex calls this finished. It also
  tests the success signal in the direction the tuned set never does: the cheap
  pass infers success from the *absence* of a failure, and here one is present.
- **`mixed`** — three segments of interleaved real work, one abandoned approach
  among them, and unrelated reads after the resolution. The tuned set's noise is
  clean generated filler.
"""

from __future__ import annotations

import json
from pathlib import Path

from seed_recurrence import build


def _write(out: Path, name: str, records: list[dict]) -> Path:
    path = out / name
    path.write_text("".join(json.dumps(r) + "\n" for r in records),
                    encoding="utf-8")
    return path


def abandoned(out: Path) -> Path:
    """Attempted, failed, reverted. One segment, `open`."""
    user, call, result, says = build([0])
    return _write(out, "cold-abandoned.jsonl", [
        user("the nightly export times out on the biggest tenant — fix it"),
        call("Read", file_path="src/export/runner.py"),
        result("120 lines"),
        call("Edit", file_path="src/export/runner.py"),
        result("edited"),
        says("Raised the statement timeout to 300s."),
        call("Bash", command="pytest tests/export -q"),
        result("2 failed, 11 passed — test_large_tenant_export: "
               "TimeoutError after 300s", error=True),
        says("Still failing, so the timeout was not the cause — the query plan "
             "is doing a sequential scan. Reverting my change; this needs an "
             "index before anything else is worth trying."),
        call("Bash", command="git checkout -- src/export/runner.py"),
        result(""),
    ])


def bespoke(out: Path) -> Path:
    """Finished through the project's own scripts. One segment, `landed`."""
    user, call, result, says = build([0])
    return _write(out, "cold-bespoke.jsonl", [
        user("put billing back on yesterday's build"),
        call("Bash", command="cat ops/rollback.sh"),
        result("#!/usr/bin/env bash\nset -euo pipefail\n# usage: rollback.sh "
               "<service> <date>"),
        call("Bash", command="./ops/rollback.sh billing 2026-08-18"),
        result("selected build 4417 (2026-08-18)\nswapping traffic...\n"
               "rollback complete: billing on 4417"),
        call("Bash", command="./ops/verify-health.sh billing"),
        result("billing: 12/12 checks passed, error rate 0.01%"),
        says("Billing is back on build 4417 and all health checks pass."),
    ])


def mutation_failed(out: Path) -> Path:
    """The mutation is refused. One segment, `open`."""
    user, call, result, says = build([0])
    return _write(out, "cold-mutation-failed.jsonl", [
        user("publish the new SDK version to the registry"),
        call("Bash", command="npm version patch"),
        result("v2.7.4"),
        call("Bash", command="npm publish"),
        result("npm ERR! code E403\nnpm ERR! 403 Forbidden - PUT "
               "https://registry.example/sdk - you do not have permission to "
               "publish 'sdk'", error=True),
        says("Blocked: this token has read scope only. Someone with registry "
             "access has to publish, or the token needs reissuing with "
             "publish rights. The version bump is committed locally and "
             "unpublished."),
    ])


def mixed(out: Path) -> Path:
    """Three segments of interleaved work: `open`, `landed`, `open`."""
    user, call, result, says = build([0])
    records = [
        # Segment 0 — an approach tried and dropped. Reads, an edit, a failing
        # benchmark, a revert. Unresolved when the developer redirects.
        user("the search results page takes four seconds — sort it out"),
        call("Bash", command="grep -rn 'def search' src/"),
        result("src/search/views.py:88: def search(request):"),
        call("Read", file_path="src/search/views.py"),
        result("140 lines"),
        call("Edit", file_path="src/search/views.py"),
        result("edited"),
        says("Added a select_related to cut the per-row queries."),
        call("Bash", command="python -m bench.search --runs 20"),
        result("p95 3.9s (was 4.0s)"),
        says("Barely moved it, so the queries were not the bottleneck. "
             "Reverting."),
        call("Bash", command="git checkout -- src/search/views.py"),
        result(""),

        # Segment 1 — the real fix, verified and committed, then unrelated
        # reads afterwards. The trailing reads are the point: position is not
        # the verdict.
        user("it's probably the ranking step, look there"),
        call("Read", file_path="src/search/rank.py"),
        result("90 lines"),
        says("It re-scores every candidate on each request with no cache."),
        call("Edit", file_path="src/search/rank.py"),
        result("edited"),
        call("Bash", command="python -m bench.search --runs 20"),
        result("p95 0.6s (was 4.0s)"),
        call("Bash", command="pytest tests/search -q"),
        result("34 passed in 4.02s"),
        call("Bash", command="git commit -am 'cache ranking scores per query'"),
        result("[main 7de1c02] cache ranking scores per query"),
        says("p95 down to 0.6s, suite green, committed."),
        call("Bash", command="git log --oneline -3"),
        result("7de1c02 cache ranking scores per query"),
        call("Bash", command="cat docs/perf-budget.md"),
        result("Search p95 budget: 1.0s"),

        # Segment 2 — a question answered by reading. Nothing carried out.
        user("is the cache going to blow up memory?"),
        call("Bash", command="grep -rn 'maxsize' src/search/rank.py"),
        result("src/search/rank.py:31: @lru_cache(maxsize=4096)"),
        says("Capped at 4096 entries, so a few MB at most. Worth revisiting if "
             "the query mix widens, but it is bounded."),
    ]
    return _write(out, "cold-mixed.jsonl", records)


# label -> why. Written before the first run and not revised on disagreement.
TRUTH: dict[str, list[tuple[str, str]]] = {
    "cold-abandoned": [
        ("open", "the change failed its tests and was reverted — a failure "
                 "that ended the attempt, not one inside finished work"),
    ],
    "cold-bespoke": [
        ("landed", "the rollback script ran and the health check passed. No "
                   "commit, no test runner, no deployment verb — nothing a "
                   "vocabulary would recognise, and it plainly finished"),
    ],
    "cold-mutation-failed": [
        ("open", "npm publish was refused with a 403. The command is in the "
                 "closing vocabulary and the work did not happen"),
    ],
    "cold-mixed": [
        ("open", "an approach tried, benchmarked, and reverted"),
        ("landed", "p95 fixed, suite green, committed — then unrelated reads "
                   "afterwards, which are not the verdict"),
        ("open", "a question answered by reading; nothing was carried out"),
    ],
}

ALL = {
    "cold-abandoned": abandoned,
    "cold-bespoke": bespoke,
    "cold-mutation-failed": mutation_failed,
    "cold-mixed": mixed,
}
