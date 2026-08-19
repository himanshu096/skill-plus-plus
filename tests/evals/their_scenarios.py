"""The capture branch's own five scenarios, plus the one it never ran.

`docs/bakeoff.md` measured that branch against fixtures written for this one,
and the fair objection is that the fixtures were the reason it lost. These are
built the other way round: from the scenario table in its own
``docs/detection.md``, deliberately on its home turf.

Concretely, on its terms rather than ours:

- **Bash, not MCP.** ``_is_closing_step`` requires ``tool == "Bash"``, so every
  finishing act here is a shell command.
- **Its closing vocabulary.** ``git commit``, ``git push``, ``npm test``,
  ``pytest``, ``ruff`` — each one a pattern that file actually matches, rather
  than a bespoke script no regex knows.
- **Inside its budgets.** No span crosses three prompts or forty steps, so
  nothing here is abandoned for size.
- **Its success utterances.** ``lgtm`` and ``looks good`` are in its
  ``_SUCCESS_RE``, and are used where a task boundary is wanted.

If this branch's detector also does well here, the earlier result is about the
architectures. If it does badly, the earlier result was about the fixtures.

**One scenario cannot be scored against both.** The two designs disagree on what
a candidate *is*: that branch banks a finished span of work, this one records a
procedure worth repeating. ``explore_then_fix`` is a one-off bug hunt, which is
a correct recipe by the first definition and correctly nothing by the second.
It is kept because trimming the leading exploration is a real property worth
testing -- but scored on that branch's axis only, and never counted as a miss
for either. Every ``EXPECTED`` entry below states both truths separately for
that reason.
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


def refine(out: Path) -> Path:
    """Refinement across two prompts — one recipe, all the steps.

    The trap this avoids on purpose: a closing step before the second prompt
    would fold the span there and produce two recipes, so the first request
    ends with an edit and nothing that looks like completion. Their table
    expects the two requests to come back as one recipe, and this is the shape
    where that can happen.
    """
    user, call, result, says = build([0])
    return _write(out, "their-refine.jsonl", [
        user("the export endpoint has no rate limit — add one"),
        call("Read", file_path="src/api/export.py"),
        result("40 lines"),
        call("Edit", file_path="src/api/export.py"),
        result("edited"),
        says("Added a limiter at 100 requests a minute."),

        user("also cap it per tenant, not just globally"),
        call("Edit", file_path="src/api/export.py"),
        result("edited"),
        call("Bash", command="npm test -- api/export"),
        result("14 passing"),
        call("Bash", command="ruff check src/api/export.py"),
        result("All checks passed!"),
        call("Bash", command="git commit -am 'rate limit exports per tenant'"),
        result("[main 3f21a0c] rate limit exports per tenant"),
        says("Both limits in, tests and lint pass, committed."),
    ])


def retry(out: Path) -> Path:
    """Failed, retried, passed — one recipe, folded after it passed.

    The failure is mid-span rather than last, which is what lets it fold at
    all. It is also the discriminator between the two designs: that branch
    keeps the failed step as a step, and this one is instructed to keep it only
    as the rule that prevents it recurring.
    """
    user, call, result, says = build([0])
    return _write(out, "their-retry.jsonl", [
        user("the staging migration is failing, get it green"),
        call("Bash", command="alembic upgrade head"),
        result("sqlalchemy.exc.OperationalError: table 'orders' is locked "
               "by an open transaction", error=True),
        says("A long-running read is holding the table. Migrations have to "
             "run with the app scaled to zero."),
        call("Bash", command="kubectl scale deploy/api --replicas=0"),
        result("deployment.apps/api scaled"),
        call("Bash", command="alembic upgrade head"),
        result("Running upgrade 4a1 -> 9c7, add orders.tenant_id"),
        call("Bash", command="kubectl scale deploy/api --replicas=3"),
        result("deployment.apps/api scaled"),
        call("Bash", command="pytest tests/migrations -q"),
        result("6 passed in 2.11s"),
        call("Bash", command="git commit -am 'add orders.tenant_id'"),
        result("[main 8ba2f19] add orders.tenant_id"),
        says("Green. Scaled the app down first, then ran it, then back up."),
    ])


def distinct_tasks(out: Path) -> Path:
    """Two distinct tasks back to back — two separate recipes.

    Separated by ``lgtm``, which is in their ``_SUCCESS_RE`` and force-folds
    the open span. Given every boundary signal their design asks for, so a
    single merged recipe here would be a failure with no excuse.
    """
    user, call, result, says = build([0])
    return _write(out, "their-distinct.jsonl", [
        user("bump CI to node 22"),
        call("Edit", file_path=".github/workflows/ci.yml"),
        result("edited"),
        call("Bash", command="npm test"),
        result("212 passing"),
        call("Bash", command="git commit -am 'CI on node 22'"),
        result("[main c40de11] CI on node 22"),
        says("Node 22 in CI, suite green, committed."),

        user("lgtm"),

        user("now cut the 2.4 release tag"),
        call("Bash", command="git merge-base --is-ancestor main release/2.4"),
        result(""),
        call("Bash", command="git tag -s v2.4.0 -m 'Release 2.4.0'"),
        result(""),
        call("Bash", command="git push --follow-tags origin release/2.4"),
        result("To github.com:acme/app.git\n * [new tag] v2.4.0 -> v2.4.0"),
        says("Confirmed release/2.4 had not diverged, signed the tag, pushed "
             "the commit and tag together."),
    ])


def explore_then_fix(out: Path) -> Path:
    """Eight greps, then the fix — their table expects the two real steps.

    **Scored on their axis only.** By their definition a finished mutation is a
    recipe; by this branch's, a one-off bug hunt is ordinary work and recording
    it is the `barren` failure. Neither answer is a miss, so what is measured
    here is only whether the leading exploration is trimmed.
    """
    user, call, result, says = build([0])
    records = [user("the export endpoint 500s intermittently — find out why")]
    for i, term in enumerate(("timeout", "retry", "ExportSerializer", "chunk",
                              "MemoryError", "buffer", "stream", "flush")):
        records += [call("Bash", command=f"grep -rn '{term}' src/"),
                    result(f"src/export/mod{i}.py:{40 + i}: match")]
    records += [
        says("The serializer buffers the whole result set before writing."),
        call("Edit", file_path="src/export/serializer.py"),
        result("edited"),
        call("Bash", command="git commit -am 'stream export rows'"),
        result("[main 91cc2de] stream export rows"),
    ]
    return _write(out, "their-explore.jsonl", records)


def mid_investigation(out: Path) -> Path:
    """Session ends mid-investigation — nothing banked, on both axes.

    The one scenario where the two designs agree exactly on the answer, which
    makes it the cleanest comparison in the set.
    """
    user, call, result, says = build([0])
    records = [user("the nightly job got slower this week — any idea why")]
    for i, cmd in enumerate((
            "git log --since='7 days ago' --oneline",
            "cat etc/cron/nightly.conf",
            "kubectl logs deploy/nightly --tail=200",
            "grep -rn 'batch_size' src/jobs/",
            "kubectl describe pod nightly-2f9x",
            "git diff HEAD~14 -- src/jobs/")):
        records += [call("Bash", command=cmd), result(f"output {i}")]
    records += [says("Nothing conclusive yet — the batch size changed but so "
                     "did the node pool. I'd need a profile to say which.")]
    return _write(out, "their-mid-investigation.jsonl", records)


def recurs(out: Path) -> Path:
    """The same procedure twice in one session — their matching, exercised.

    Not from their table. Added because every fixture in `docs/bakeoff.md`
    banked at ×1, so their lexical dedup and their 3× threshold were never
    reached by anything, and a design that counts recurrences was compared
    without its counting ever running.

    Two runs of one procedure, worded differently, on different services, with
    a confirmation between them so each is unambiguously its own span.
    """
    user, call, result, says = build([0])
    records: list[dict] = []
    for ask, svc, ver in (("roll out the api hotfix to staging", "api", "1.9.3"),
                          ("same rollout for the worker please", "worker", "2.2.1")):
        records += [
            user(ask),
            call("Bash", command=f"kubectl get deploy/{svc} -o "
                                 "jsonpath='{.spec.replicas}'"),
            result("3"),
            call("Bash", command=f"helm upgrade {svc} charts/{svc} "
                                 f"--set image.tag={ver} --wait"),
            result(f"Release \"{svc}\" has been upgraded. STATUS: deployed"),
            call("Bash", command=f"kubectl rollout status deploy/{svc} "
                                 "--timeout=120s"),
            result(f"deployment \"{svc}\" successfully rolled out"),
            says(f"{svc} on {ver}, rollout complete, replicas back to 3."),
            user("looks good"),
        ]
    return _write(out, "their-recurs.jsonl", records)


# Ground truth, stated separately per design because they disagree on what a
# candidate is. `theirs` is from their own scenario table; `mine` is what
# /log-session should conclude under this branch's rules.
EXPECTED = {
    "refine": {"theirs": "1 recipe, all steps across both prompts",
               "mine": "1 entry — the two requests are one procedure"},
    "retry": {"theirs": "1 recipe, folded after the pass",
              "mine": "1 entry, and the lock failure kept as a rule "
                      "(scale to zero first), not as an account of it"},
    "distinct-tasks": {"theirs": "2 recipes", "mine": "2 entries, low overlap"},
    "explore-then-fix": {"theirs": "1 recipe of 2 steps, 8 greps trimmed",
                         "mine": "not scored — a one-off fix is ordinary work "
                                 "by this branch's rules"},
    "mid-investigation": {"theirs": "nothing banked", "mine": "nothing recorded"},
    "recurs": {"theirs": "1 recipe at x2", "mine": "1 entry at x2"},
}

ALL = {
    "refine": refine,
    "retry": retry,
    "distinct-tasks": distinct_tasks,
    "explore-then-fix": explore_then_fix,
    "mid-investigation": mid_investigation,
    "recurs": recurs,
}
