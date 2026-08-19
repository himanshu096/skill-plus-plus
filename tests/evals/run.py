#!/usr/bin/env python3
"""Evals for the parts of session review a unit test cannot reach.

    python3 tests/evals/run.py            # every case
    python3 tests/evals/run.py --case new # one of them
    python3 tests/evals/run.py --keep     # leave the scratch roots for reading

Separate from ``tests/`` on purpose. Those run in under two seconds and are
free; each case here spends a real model call, so this is a thing you fire
after changing a prompt, not on every save.

**What belongs here and what does not.** A case earns its place only if the
answer depends on judgement. Whether a count rises, whether a body survives a
write, whether the threshold filters -- all of that is decided by code with one
right answer, and it belongs in ``tests/test_skillpp.py`` where it costs
nothing. What is left is the question the model is actually there for: *is this
procedure worth recording, and is it the same as one already stored?*

**The command file is not reimplemented here.** Each case shells out to the
real ``/log-session``, sandboxed by ``SKILLPP_ROOT`` alone. An eval that
reconstructs the prompt tests the reconstruction, and the two drift the first
time someone edits the command file without touching this one.

Costs roughly a few cents per case. Failures print the model's own transcript,
because the interesting part of a judgement failure is always the reasoning.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "tests" / "fixtures"), str(Path(__file__).parent)]

from skillpp.config import Config          # noqa: E402
from skillpp.memory import add_entry, add_occurrence  # noqa: E402

RECURRENCE_A = REPO / "tests" / "fixtures" / "recurrence-a.jsonl"
RECURRENCE_B = REPO / "tests" / "fixtures" / "recurrence-b.jsonl"

# A procedure with nothing in common with the fixtures, so a model matching
# against it is matching on "both are procedures" rather than on the work.
UNRELATED = """Check the release branch is behind main before cutting a tag.

## Confirm the branch point

Run `git merge-base --is-ancestor main release` first. A tag cut from a branch
that has diverged ships code review never saw.

## Tag and push atomically

`git tag -s` then `git push --follow-tags`. Pushing the tag separately leaves a
window where the release exists and its commit does not.
"""


@dataclass
class Case:
    name: str
    asks: str
    breaks: str
    # A list, because some judgements only exist between two reviews. The
    # match case cannot be one pass: a single review of a conversation that
    # repeats a procedure sees one procedure, correctly. Matching is what the
    # *second* review does against what the first one stored.
    transcripts: Callable[[Path], list[Path]]
    check: Callable[[list[dict]], str | None]
    seed: Callable[[Config], None] | None = None


def _entry(entries: list[dict], name_part: str) -> dict | None:
    return next((e for e in entries if name_part in e["name"]), None)


# ---------------------------------------------------------------- the cases

# Values that belong to the one run the fixture describes. A body carrying
# any of them is a record of what happened rather than instructions for what
# to do, and the skill built from it tells a future agent to file OPS-4471.
INSTANCE = ("OPS-4471", "88213", "support.example", "ExportSerializer")

# The rules that run actually established, any one of which shows the body
# caught the reasoning and not just the sequence of calls. Deliberately a
# generous set: asserting on exact prose makes a judgement eval flaky, and a
# flaky eval gets ignored, which is worse than not having it.
RULES = ("signature", "duplicate", "rubric", "catalogue", "severity")


def _quality(body: str) -> str | None:
    leaked = [v for v in INSTANCE if v in body]
    if leaked:
        return f"body keeps this run's details: {leaked}"
    if not any(r in body.lower() for r in RULES):
        return f"body has the steps but none of the rules: {body[:200]!r}"
    return None


def _records_something(entries: list[dict]) -> str | None:
    if len(entries) != 1:
        return f"expected 1 entry, got {len(entries)}: {[e['name'] for e in entries]}"
    body = entries[0]["body"]
    if len(body.splitlines()) < 3:
        return f"body is one line, not instructions: {body!r}"
    if entries[0]["count"] != 1:
        return f"count is {entries[0]['count']}, expected 1"
    return _quality(body)


def _found_it_in_the_noise(entries: list[dict]) -> str | None:
    # Recall and precision at once. One procedure finished in that session;
    # anything beyond it is the grep-and-benchmark filler mistaken for a
    # pattern, which is the failure more material actually causes.
    if not entries:
        return "found nothing — the procedure did not survive the noise"
    if len(entries) > 1:
        return (f"found {len(entries)}, expected the one that finished: "
                f"{[e['name'] for e in entries]}")
    return _quality(entries[0]["body"])


def _two_procedures(entries: list[dict]) -> str | None:
    # Deliberately not keyword matching. The first version of this check
    # required the word "ticket" and failed a body that said "issue" and
    # "tracker" throughout -- a correct body, failed on vocabulary. An eval
    # that fails on synonyms gets muted, and a muted eval is worse than none,
    # so the property asserted here is the structural one: two entries that
    # are actually about different things.
    if len(entries) != 2:
        return (f"expected both procedures, got {len(entries)}: "
                f"{[e['name'] for e in entries]}")
    if entries[0]["name"] == entries[1]["name"]:
        return "one procedure recorded twice"
    thin = [e["name"] for e in entries if len(e["body"].splitlines()) < 3]
    if thin:
        return f"recorded but not written as instructions: {thin}"

    first, second = (set(e["body"].lower().split()) for e in entries)
    overlap = len(first & second) / len(first | second)
    if overlap > 0.5:
        return (f"the two bodies are {overlap:.0%} the same words — one "
                f"procedure split in half rather than two found")
    return None


def _matched_itself(entries: list[dict]) -> str | None:
    # Two occurrences of one procedure inside one conversation. Two entries at
    # x1 means --matches was never reached for at all.
    if len(entries) != 1:
        return (f"expected the two occurrences folded into 1 entry, got "
                f"{len(entries)}: {[(e['name'], e['count']) for e in entries]}")
    if entries[0]["count"] < 2:
        return f"count is {entries[0]['count']}, expected 2"
    return None


def _kept_them_apart(entries: list[dict]) -> str | None:
    # The dangerous direction. A wrong match is silent: it inflates one count,
    # discards the proposal, and leaves no trace that two different procedures
    # were treated as one.
    seeded = _entry(entries, "cutting-a-release-tag")
    if seeded is None:
        return "the seeded entry vanished"
    if seeded["count"] != 1:
        return (f"matched the unrelated procedure — 'cutting-a-release-tag' "
                f"rose to x{seeded['count']}")
    if len(entries) != 2:
        return f"expected the seeded entry plus a new one, got {len(entries)}"
    return None


def _recorded_nothing(entries: list[dict]) -> str | None:
    if entries:
        return (f"recorded {len(entries)} from ordinary work: "
                f"{[e['name'] for e in entries]}")
    return None


def _seed_unrelated(config: Config) -> None:
    add_entry(config, name="cutting-a-release-tag", body=UNRELATED)
    add_occurrence(config, name="cutting-a-release-tag", session="seeded")


def _mundane(out: Path) -> Path:
    from mundane import transcript
    return transcript(out)


def _session(which: str, out: Path) -> Path:
    import sessions
    return getattr(sessions, which)(out)


CASES = [
    Case("new",
         asks="a session containing one completed procedure, empty store",
         breaks="nothing is ever recorded, and the store stays empty forever",
         transcripts=lambda out: [RECURRENCE_A],
         check=_records_something),
    Case("match",
         asks="two reviews of one conversation that did the procedure twice",
         breaks="every recurrence files as new, counts freeze at x1, and the "
                "threshold is never reached by anything",
         transcripts=lambda out: [RECURRENCE_A, RECURRENCE_B],
         check=_matched_itself),
    Case("distinct",
         asks="a store holding an unrelated procedure, plus a new one",
         breaks="two different procedures merge into one entry — silently, "
                "and the discarded proposal is not recoverable",
         transcripts=lambda out: [RECURRENCE_A],
         seed=_seed_unrelated,
         check=_kept_them_apart),
    Case("big",
         asks="one finished procedure buried in 433 KB of unrelated work",
         breaks="the detector works only on toy sessions — it either loses "
                "the procedure in the noise or promotes the noise",
         transcripts=lambda out: [_session("big", out)],
         check=_found_it_in_the_noise),
    Case("incomplete",
         asks="the same procedure, abandoned when the tracker went down",
         breaks="skills get written from routes nobody ever walked to the end",
         transcripts=lambda out: [_session("incomplete", out)],
         check=_recorded_nothing),
    Case("two",
         asks="two unrelated procedures, both finished, in one session",
         breaks="only the strongest is ever considered, and the session is "
                "bookmarked as read either way — so the second is not "
                "rejected, it is lost",
         transcripts=lambda out: [_session("two", out)],
         check=_two_procedures),
    Case("barren",
         asks="a long session of ordinary git work",
         breaks="the store fills with 'running-the-test-suite' and stops "
                "being read",
         transcripts=lambda out: [_mundane(out)],
         check=_recorded_nothing),
]


# ------------------------------------------------------------------ running

def store(root: Path) -> list[dict]:
    env = {**os.environ, "SKILLPP_ROOT": str(root)}
    out = subprocess.run([sys.executable, "bin/skillpp", "candidates", "--json"],
                         cwd=REPO, env=env, capture_output=True, text=True)
    text = out.stdout.strip()
    return json.loads(text) if text.startswith("[") else []


def run(case: Case, *, keep: bool) -> tuple[bool, str, Path, str]:
    root = Path(tempfile.mkdtemp(prefix=f"skillpp-eval-{case.name}-"))
    config = Config(root)
    if case.seed:
        case.seed(config)
    env = {**os.environ, "SKILLPP_ROOT": str(root)}
    said, why = [], ""
    for path in case.transcripts(root):
        proc = subprocess.run(
            ["claude", "-p", f"/log-session {path}",
             "--no-session-persistence",
             "--allowed-tools", "Bash(python3 bin/skillpp *)"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=600)
        said.append(proc.stdout)
        if proc.returncode != 0:
            why = f"claude exited {proc.returncode}: {proc.stderr.strip()[:400]}"
            break

    why = why or case.check(store(root)) or ""
    if not why and not keep:
        shutil.rmtree(root, ignore_errors=True)
    return (not why), why, root, "\n--- next review ---\n".join(said)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", action="append", help="run only these")
    ap.add_argument("--keep", action="store_true",
                    help="keep scratch roots even when a case passes")
    args = ap.parse_args()

    picked = [c for c in CASES if not args.case or c.name in args.case]
    if not picked:
        print(f"no such case. have: {', '.join(c.name for c in CASES)}")
        return 2

    failures = []
    for case in picked:
        print(f"… {case.name}: {case.asks}", flush=True)
        ok, why, root, said = run(case, keep=args.keep)
        if ok:
            print(f"  ok\n")
        else:
            failures.append(case.name)
            print(f"  FAILED — {why}")
            print(f"  breaks: {case.breaks}")
            print(f"  root:   {root}")
            print("  the model said:")
            print("".join(f"    {ln}\n" for ln in said.strip().splitlines()[:25]))

    print(f"{len(picked) - len(failures)}/{len(picked)} passed"
          + (f" — failed: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
