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

import locator                              # noqa: E402
from skillpp.config import Config          # noqa: E402
from skillpp.memory import (PROMOTED, add_entry, add_occurrence,  # noqa: E402
                            record_decision)

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


NEARBY = """Escalate a sev-1 the moment it is confirmed, before diagnosing.

## Confirm scope before paging

Check the error rate is service-wide and not one tenant. Paging on a single
tenant's outage burns the rotation and the next real sev-1 is answered slower.

## Page, then investigate

Open the incident channel and page the on-call rota *before* looking for a
cause. Diagnosis while unpaged is the single longest delay in every postmortem
we have run.
"""

# What the store already knows when a procedure recurs after being promoted.
ALREADY = """Read the intake spec before filing anything — the required fields
change and a remembered list goes stale.

## Search before filing

Search the tracker by the stack-trace signature, not the report's title.
Titles get phrased differently every time; a title search misses duplicates a
signature search finds.

## Link, do not duplicate

If a match exists, attach the source thread to it rather than filing a second
copy, and post the tracker link back into the thread.
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
    check: Callable[[list[dict]], str | None] | None = None
    seed: Callable[[Config], None] | None = None
    command: str = "/log-session"
    # AskUserQuestion is withheld from the review cases deliberately. What can
    # be observed without a human is everything up to the question: which
    # candidates the queue offered, and that nothing was written before an
    # answer existed. Answering it would mean stubbing the tool and asserting
    # on the stub.
    tools: str = "Bash(python3 bin/skillpp *)"
    denials_expected: bool = False
    bookmarks: bool = True
    # Lowers the new-message floor for this case. That floor is a cost guard --
    # a small increment is not worth a model call, and the messages are not
    # lost because the bookmark does not advance. An eval is the one caller
    # that deliberately pays the cost, so a fixture built small on purpose
    # would otherwise be answered "stop" without the judgement ever being put
    # to the model. Only set it where the fixture is small by design.
    min_messages: int | None = None
    # Appended after the transcript path in the prompt, so it reaches the
    # command file's own `!` line. Used to withhold aspects for the ablation:
    # same instructions, different input, which is what makes it a comparison
    # rather than two experiments.
    args: str = ""
    # Extra environment for this case. Preferred over `args` whenever the
    # command template runs more than one command: `$ARGUMENTS` is substituted
    # into all of them, so a flag only one accepts breaks the rest.
    env: dict[str, str] = field(default_factory=dict)
    check_run: Callable[[Path, str, list[dict]], str | None] | None = None
    # Cold discovery runs somewhere else entirely: a throwaway project holding
    # the skill, so Claude Code discovers it the way it would in real work.
    # SKILLPP_SKILLS_DIR is no help here -- that is where skillpp *writes*, and
    # this is about where Claude Code *reads*.
    project: Callable[[Path], Path] | None = None
    check_stream: Callable[[str], str | None] | None = None


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


def _quality(body: str, rules: tuple[str, ...] = RULES) -> str | None:
    """Instance details out, at least one rule in.

    ``rules`` is a parameter because the default set is the bug-filing
    fixture's vocabulary, and reusing it on another fixture fails a correct
    body on words that could not possibly appear in it -- a rate-limiting
    procedure has no "signature" or "catalogue" in it, and a check that
    demands one is testing which fixture it was pointed at.
    """
    leaked = [v for v in INSTANCE if v in body]
    if leaked:
        return f"body keeps this run's details: {leaked}"
    if not any(r in body.lower() for r in rules):
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
        return (f"recorded {len(entries)}: {[e['name'] for e in entries]}")
    return None


def _bookmarked(root: Path) -> str | None:
    """Finding nothing still has to be written down.

    The hole this closes: every case so far asserted on the store, and a store
    that stays empty looks identical whether the session was reviewed and
    dismissed or never reviewed at all. Skip the bookmark and those messages
    arrive again in the next batch, forever.
    """
    reviews = list((root / "reviews").glob("*.md"))
    if not reviews:
        return "no review written — the session will be read again forever"
    if not any("last_message:" in r.read_text() for r in reviews):
        return "review written without a bookmark; the messages stay unread"
    return None


def _redacted(entries: list[dict]) -> str | None:
    if len(entries) != 1:
        return f"expected 1 entry, got {len(entries)}: {[e['name'] for e in entries]}"
    body = entries[0]["body"]
    if "dsh_live_4Kq9wRt2bNm7xZ" in body:
        return "the credential is in the body verbatim"
    if "dsh_live" in body:
        return "part of the credential survived into the body"
    return None


def _stayed_promoted(entries: list[dict]) -> str | None:
    if len(entries) != 1:
        return (f"filed a duplicate of a promoted skill instead of matching it: "
                f"{[(e['name'], e['status']) for e in entries]}")
    entry = entries[0]
    if entry["count"] != 2:
        return f"count is {entry['count']}, expected the sighting to be logged"
    if entry["status"] != "promoted":
        return f"status fell back to {entry['status']!r}"
    return None


def _kept_the_neighbour_apart(entries: list[dict]) -> str | None:
    seeded = _entry(entries, "escalating-a-sev-1")
    if seeded is None:
        return "the seeded entry vanished"
    if seeded["count"] != 1:
        return (f"matched a different procedure from the same domain — "
                f"'escalating-a-sev-1' rose to x{seeded['count']}")
    if len(entries) != 2:
        return f"expected the seeded entry plus a new one, got {len(entries)}"
    return None


def _limit_rules(entries: list[dict]) -> str | None:
    if len(entries) != 1:
        return (f"expected 1 entry, got {len(entries)}: "
                f"{[e['name'] for e in entries]}")
    return _quality(entries[0]["body"],
                    ("tenant", "per-tenant", "scope", "global"))


def _generalised_across_both_runs(entries: list[dict]) -> str | None:
    """Two runs of one procedure become one entry, written for neither.

    Third version of this check, and the first that asserts something the
    fixture can actually show. It demanded ``count == 2`` (unreachable -- count
    is distinct sessions), then two lines in ``occurrences.jsonl`` (which the
    model produces or not, run to run, and which change nothing either way
    because sessions are deduplicated on read). Both were chasing a number
    rather than the property.

    The property is generalisation. Two rollouts differing only in service name
    and image tag should yield one entry whose body names neither, because a
    body hardcoding ``api`` was written from one run and did not recognise the
    other. That is the step the capture branch never reached -- it banked three
    separate entries.
    """
    if len(entries) != 1:
        return (f"expected the two rollouts folded into 1 entry, got "
                f"{len(entries)}: {[(e['name'], e['count']) for e in entries]}")
    body = entries[0]["body"]
    hardcoded = [s for s in ("deploy/api", "deploy/worker", "charts/api",
                             "charts/worker", "1.9.3", "2.2.1") if s in body]
    if hardcoded:
        return (f"the body was written from one run rather than both: "
                f"{hardcoded}")
    if "<" not in body:
        return f"nothing parameterised — not a reusable body: {body[:200]!r}"
    return None


def _did_not_merge_the_two(root: Path, said: str,
                           entries: list[dict]) -> str | None:
    """Scored on this branch's definition, not head-to-head.

    Their table expects two recipes because any finished mutation span is one.
    Under this branch's rules a CI version bump is edit/test/commit with no rule
    worth writing down, and recording it is the `barren` failure -- so one entry
    here is a defensible answer and two is also defensible. What is *not* is
    merging them, which is the silent failure: one inflated count and a
    discarded proposal.
    """
    tag = _entry(entries, "release-tag") or _entry(entries, "tag")
    if tag is None:
        return (f"the release-tag procedure was not recorded: "
                f"{[e['name'] for e in entries]}")
    if len(entries) > 2:
        return f"fragmented into {len(entries)}: {[e['name'] for e in entries]}"
    if len(entries) == 2:
        first, second = (set(e["body"].lower().split()) for e in entries)
        overlap = len(first & second) / len(first | second)
        if overlap > 0.5:
            return (f"the two bodies are {overlap:.0%} the same words — one "
                    f"procedure split rather than two found")
    return None


def _seed_unrelated(config: Config) -> None:
    add_entry(config, name="cutting-a-release-tag", body=UNRELATED)
    add_occurrence(config, name="cutting-a-release-tag", session="seeded")


def _seed_nearby(config: Config) -> None:
    add_entry(config, name="escalating-a-sev-1-incident", body=NEARBY)
    add_occurrence(config, name="escalating-a-sev-1-incident", session="seeded")


def _seed_promoted(config: Config) -> None:
    add_entry(config, name="filing-a-bug-from-a-support-report", body=ALREADY)
    add_occurrence(config, name="filing-a-bug-from-a-support-report",
                   session="seeded")
    record_decision(config, name="filing-a-bug-from-a-support-report",
                    action=PROMOTED, skill_path="/tmp/does-not-matter/SKILL.md")


def _promotions(root: Path) -> list[str]:
    log = root / "decisions.jsonl"
    return log.read_text().splitlines() if log.exists() else []


def _skills_written(root: Path) -> list[str]:
    return [p.name for p in (root / "skills").glob("*/SKILL.md")]


def _asked_nothing_of_an_empty_queue(root: Path, said: str,
                                     entries: list[dict]) -> str | None:
    """Below the threshold is not a question, and must not become one."""
    if _promotions(root):
        return f"promoted from an empty queue: {_promotions(root)}"
    if _skills_written(root):
        return f"wrote a skill with nothing waiting: {_skills_written(root)}"
    if len(entries) != 2:
        return f"the store changed: {[(e['name'], e['count']) for e in entries]}"
    return None


def _offered_all_of_them(root: Path, said: str,
                         entries: list[dict]) -> str | None:
    """The bug this was built for: only the strongest was ever shown.

    A queue that hands back one candidate and stops is not a queue. The others
    are not rejected -- they are never mentioned, so nobody knows to ask.
    """
    missing = [e["name"] for e in entries if e["name"] not in said]
    if missing:
        return f"waiting but never mentioned: {missing}"
    if _promotions(root):
        return f"promoted without being answered: {_promotions(root)}"
    if _skills_written(root):
        return f"wrote a skill before any answer: {_skills_written(root)}"
    return None


def _seed_below(config: Config) -> None:
    for name, body, seen in (("cutting-a-release-tag", UNRELATED, 1),
                             ("escalating-a-sev-1-incident", NEARBY, 2)):
        add_entry(config, name=name, body=body)
        for n in range(seen):
            add_occurrence(config, name=name, session=f"{name}-{n}")


def _seed_queue(config: Config) -> None:
    for name, body in (("cutting-a-release-tag", UNRELATED),
                       ("escalating-a-sev-1-incident", NEARBY),
                       ("filing-a-bug-from-a-support-report", ALREADY)):
        add_entry(config, name=name, body=body)
        for n in range(3):
            add_occurrence(config, name=name, session=f"{name}-{n}")


def _multitask(which: str, out: Path) -> Path:
    import multitask
    return multitask.ALL[which][0](out)


def _counted(want: int):
    """Exactly `want` distinct procedures, and none of them a duplicate.

    The count is the whole point of these cases, so the check reports what it
    found rather than only that it was wrong — a run that banks 1 and a run
    that banks 4 fail for opposite reasons and want different fixes.
    """
    def check(entries: list[dict]) -> str | None:
        if len(entries) != want:
            return (f"banked {len(entries)}, expected {want}: "
                    f"{[e['name'] for e in entries]}")
        thin = [e["name"] for e in entries
                if len(e["body"].splitlines()) < 3]
        if thin:
            return f"recorded but not written as instructions: {thin}"
        # Two entries naming the same work is the failure the self-match fix
        # was about, and it can recur without the count changing.
        for i, a in enumerate(entries):
            for b in entries[i + 1:]:
                first = set(a["body"].lower().split())
                second = set(b["body"].lower().split())
                overlap = len(first & second) / len(first | second)
                if overlap > 0.5:
                    return (f"{a['name']} and {b['name']} are {overlap:.0%} the "
                            f"same words — one procedure split, not two found")
        return None
    return check


def _cold(which: str, out: Path) -> Path:
    import cold
    return cold.ALL[which](out)


def _cold_check(name: str):
    """Score a /locate reply against a cold fixture's pre-committed labels.

    Separate from `_locator_check` only in where the truth comes from. The
    labels in `cold.py` were written before anything was run and are not to be
    revised because an answer disagrees -- that is the whole point of having
    them, since the tuned set's prompt was revised three times against it.
    """
    def check(root: Path, said: str, entries: list[dict]) -> str | None:
        import cold
        import locator
        if entries:
            return (f"wrote {len(entries)} candidate(s) — the locator answers "
                    f"and stops: {[e['name'] for e in entries]}")
        got = locator.parse(said)
        truth = cold.TRUTH[name]
        if not got:
            return f"no verdicts in the reply: {said.strip()[:200]!r}"
        missing = [i for i in range(len(truth)) if i not in got]
        if missing:
            return f"segments not answered: {missing}"
        wrong = [f"{i}: said {got[i]}, is {want} ({why})"
                 for i, (want, why) in enumerate(truth) if got[i] != want]
        if wrong:
            return f"{len(wrong)}/{len(truth)} wrong — " + "; ".join(wrong)
        return None
    return check


def _locator_check(name: str):
    """Score a /locate reply against the labelled truth for one fixture.

    Also guards that nothing was written. The locator is asked to answer and
    stop; a reply that also records a candidate has done the frontier judge's
    job on a fraction of the context, which is the failure mode the whole split
    exists to prevent.
    """
    def check(root: Path, said: str, entries: list[dict]) -> str | None:
        import locator
        if entries:
            return (f"wrote {len(entries)} candidate(s) — the locator answers "
                    f"and stops: {[e['name'] for e in entries]}")
        return locator.score(name, said)
    return check


def _their(which: str, out: Path) -> Path:
    import their_scenarios
    return their_scenarios.ALL[which](out)


def _kept_the_lock_rule(entries: list[dict]) -> str | None:
    """One entry, and the failure survives as the rule that prevents it.

    The discriminator between the two designs on the same session. That branch
    keeps the failed `alembic upgrade` as a step; this one is instructed to keep
    it only as the rule -- scale the app to zero before migrating -- because a
    body that recounts the failure tells a future agent to reproduce it.
    """
    if len(entries) != 1:
        return (f"expected 1 entry, got {len(entries)}: "
                f"{[e['name'] for e in entries]}")
    body = entries[0]["body"].lower()
    if not any(w in body for w in ("scale", "replicas", "zero", "0")):
        return f"the lock rule is missing: {entries[0]['body'][:200]!r}"
    return None


PROMOTED_SKILL = (Path.home() / ".claude" / "skills"
                  / "bootstrapping-an-ephemeral-test-runner" / "SKILL.md")


def _cold(out: Path) -> Path:
    import sessions
    return sessions.cold_project(out, PROMOTED_SKILL)


def _fired(stream: str) -> str | None:
    called = _skills_invoked(stream)
    if PROMOTED_SKILL.parent.name not in called:
        return (f"the skill was on disk and was not reached for; "
                f"skills called: {called or 'none'}")
    return None


def _stayed_quiet(stream: str) -> str | None:
    # Without this, a description matching everything passes the fire test.
    # Over-firing is not a harmless error: it costs a load of the body on
    # every unrelated request, and it teaches the developer to ignore skills.
    called = _skills_invoked(stream)
    if PROMOTED_SKILL.parent.name in called:
        return "fired on a request it does not cover — the description is too broad"
    return None


# What Claude Code says when it declines to run something, as opposed to
# running it and getting a non-zero exit.
REFUSALS = ("simple_expansion", "permission", "not allowed", "requires approval",
            "user doesn't want", "was rejected")


def _failed_calls(stream: str) -> list[str]:
    """Tool calls that came back an error.

    The signal verification actually needs. A skill that *fails* is easy to
    notice; the dangerous one succeeds while the agent quietly improvises
    around a gap in it, and then the gap is never found. That is exactly what
    the first promoted skill did: it fired, ran its prescribed command, hit a
    collection error, recovered with `PYTHONPATH=.`, and reported success. By
    every naive measure that run passed.
    """
    bad = []
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{") or "tool_result" not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for block in event.get("message", {}).get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            if not block.get("is_error"):
                continue
            body = block.get("content")
            if isinstance(body, list):
                body = " ".join(str(b.get("text", "")) for b in body
                                if isinstance(b, dict))
            body = str(body)
            # A refusal is the allowlist being too tight, not the skill being
            # wrong. Counting the two together said the first promoted skill
            # errored five times when it had in fact run correctly -- the
            # third time in this harness that an environment problem was
            # reported as a judgement.
            if any(m in body.lower() for m in REFUSALS):
                continue
            bad.append(body[:160])
    return bad


def _kept_its_promise(root: Path, said: str,
                      entries: list[dict]) -> str | None:
    """Its own claim, in its own description: nothing persists.

    A skill that leaves a venv behind is not wrong about the tests -- it is
    wrong about the thing it was promoted for.
    """
    project = root / "coldproject"
    left = [p.name for p in project.glob(".venv")] + \
           [p.name for p in project.glob("*.egg-info")]
    if left:
        return f"promised nothing would persist and left {left}"
    return None


# Looking around is not retrying. `ls dir` followed by `ls dir/tests` is one
# substring of another and means nothing; the same shape in the command that
# does the work is the signal.
BROWSING = {"ls", "cat", "which", "find", "head", "tail", "grep", "pwd",
            "echo", "wc", "stat", "file", "command", "type", "test"}


def _commands(stream: str) -> list[str]:
    out = []
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{") or '"Bash"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for block in event.get("message", {}).get("content", []):
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and block.get("name") == "Bash"):
                cmd = str((block.get("input") or {}).get("command", "")).strip()
                if not cmd:
                    continue
                first = cmd.split()[0].split("/")[-1] if cmd.split() else ""
                if first in BROWSING:
                    continue
                out.append(cmd)
    return out


def _worked_as_written(stream: str) -> str | None:
    """Fired, finished, and needed no improvisation.

    All three read the raw event stream. An earlier version asked whether the
    skill fired from ``_spoken`` output instead, which holds only assistant
    prose -- so a skill that had plainly fired was reported as never called.
    """
    if PROMOTED_SKILL.parent.name not in _skills_invoked(stream):
        return f"never fired; called: {_skills_invoked(stream) or 'none'}"
    if "passed" not in _spoken(stream).lower():
        return "no passing test run — the procedure did not finish"
    return _no_retry(stream)


def _no_retry(stream: str) -> str | None:
    """Did the agent have to re-run the skill's command, changed?

    Exit codes were tried first and were wrong in both directions. Too strict:
    the skill's own probe, `which uv pipx pip3`, exits 1 whenever one of them
    is absent -- which is the probe working, not failing. Too lax: its test
    command ends `2>&1 | tail -80`, so the pipeline exits 0 even when pytest
    fails collection, and the one real defect was invisible.

    A retry survives both. If the agent runs a command and then runs that same
    command with something added, the first attempt did not do the job, and
    whatever it added is the rule the skill should have carried.
    """
    seen = _commands(stream)
    for i, earlier in enumerate(seen):
        for later in seen[i + 1:]:
            if earlier != later and earlier in later:
                added = later.replace(earlier, "").strip()
                return (f"had to re-run its own command with {added!r} added — "
                        f"that belongs in the skill, not in the recovery")
    return None


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
    Case("near",
         asks="a store holding a different procedure from the same domain",
         breaks="the false match nobody catches — `distinct` only rules out "
                "matching things with nothing in common",
         transcripts=lambda out: [RECURRENCE_A],
         seed=_seed_nearby,
         check=_kept_the_neighbour_apart),
    Case("promoted",
         asks="the procedure recurs after it was already made into a skill",
         breaks="a duplicate candidate for a skill that already exists, and "
                "the evidence the skill earns its place is lost",
         transcripts=lambda out: [RECURRENCE_A],
         seed=_seed_promoted,
         check=_stayed_promoted),
    Case("secrets",
         asks="a finished procedure whose commands carry a live token",
         breaks="the credential lands in a SKILL.md, which is the artifact "
                "that gets committed and shared",
         transcripts=lambda out: [_session("secrets", out)],
         check=_redacted),
    Case("stop",
         asks="the same session reviewed twice, nothing new the second time",
         breaks="every re-review re-records, and counts inflate without the "
                "procedure ever recurring",
         transcripts=lambda out: [RECURRENCE_A, RECURRENCE_A],
         check=_records_something),
    Case("review-empty",
         asks="/review-candidates with everything below the threshold",
         breaks="the threshold gates the listing but not the promotion, so a "
                "procedure seen once can still be made into a skill",
         transcripts=lambda out: [],
         seed=_seed_below,
         command="/review-candidates",
         denials_expected=True, bookmarks=False,
         check_run=_asked_nothing_of_an_empty_queue),
    Case("review-lists-all",
         asks="/review-candidates with three candidates past the threshold",
         breaks="only the strongest is offered — the rest are not rejected, "
                "they are never mentioned",
         transcripts=lambda out: [],
         seed=_seed_queue,
         command="/review-candidates",
         denials_expected=True, bookmarks=False,
         check_run=_offered_all_of_them),
    # ---- the capture branch's own scenarios, on its home turf ----
    # docs/bakeoff.md measured that branch against fixtures written for this
    # one, and the fair objection was that the fixtures decided it. These are
    # built from its own scenario table with its own closing vocabulary. Its
    # score on them is in that document; these are the other column.
    Case("their-refine",
         asks="one procedure refined across two requests, its way",
         breaks="a procedure that took two requests is recorded as two",
         transcripts=lambda out: [_their("refine", out)],
         min_messages=1,
         check=_limit_rules),
    Case("their-retry",
         asks="a migration that failed on a lock, then passed",
         breaks="the failure is recorded as something that happened rather "
                "than as the rule that stops it happening again",
         transcripts=lambda out: [_their("retry", out)],
         min_messages=1,
         check=_kept_the_lock_rule),
    Case("their-distinct",
         asks="two tasks back to back, separated by 'lgtm'",
         breaks="given every boundary signal there is, they still merge or "
                "fragment",
         transcripts=lambda out: [_their("distinct-tasks", out)],
         min_messages=1,
         check_run=_did_not_merge_the_two),
    Case("their-explore",
         asks="eight greps then a one-line fix, committed",
         breaks="ordinary debugging is recorded as a procedure, which is how "
                "the store fills with things nobody reads",
         # Scored against this branch's definition, not head-to-head: that
         # branch is right to bank a finished mutation here, and this one is
         # right to record nothing. See their_scenarios.explore_then_fix.
         transcripts=lambda out: [_their("explore-then-fix", out)],
         min_messages=1,
         check=_recorded_nothing),
    Case("their-mid",
         asks="six reads and no conclusion — the one both designs agree on",
         breaks="an abandoned investigation becomes a candidate",
         transcripts=lambda out: [_their("mid-investigation", out)],
         min_messages=1,
         check=_recorded_nothing),
    Case("their-recurs",
         asks="the same rollout run twice in one session, two services",
         breaks="an identical procedure repeated does not count as a "
                "recurrence, so no threshold is ever reached",
         transcripts=lambda out: [_their("recurs", out)],
         min_messages=1,
         check=_generalised_across_both_runs),
    # ---- the locator question, on a frontier model first ----
    # Per segment: did the work reach a resolved state? Deliberately not "is it
    # worth keeping", which needs the whole span. Tested at the ceiling before
    # anything is built on a 7B model: if a frontier model cannot answer it,
    # the architecture that puts this step locally is dead.
    Case("locate-refine",
         asks="per-segment verdicts for the their-refine fixture",
         breaks="the locator cannot tell finished work from unresolved work, "
                "so every span it hands on is guesswork",
         transcripts=lambda out: [_their("refine", out)],
         command="/locate", bookmarks=False,
         check_run=_locator_check("their-refine")),
    Case("locate-retry",
         asks="per-segment verdicts for the their-retry fixture",
         breaks="the locator cannot tell finished work from unresolved work, "
                "so every span it hands on is guesswork",
         transcripts=lambda out: [_their("retry", out)],
         command="/locate", bookmarks=False,
         check_run=_locator_check("their-retry")),
    Case("locate-distinct",
         asks="per-segment verdicts for the their-distinct fixture",
         breaks="the locator cannot tell finished work from unresolved work, "
                "so every span it hands on is guesswork",
         transcripts=lambda out: [_their("distinct-tasks", out)],
         command="/locate", bookmarks=False,
         check_run=_locator_check("their-distinct")),
    Case("locate-explore",
         asks="per-segment verdicts for the their-explore fixture",
         breaks="the locator cannot tell finished work from unresolved work, "
                "so every span it hands on is guesswork",
         transcripts=lambda out: [_their("explore-then-fix", out)],
         command="/locate", bookmarks=False,
         check_run=_locator_check("their-explore")),
    Case("locate-mid-investigation",
         asks="per-segment verdicts for the their-mid-investigation fixture",
         breaks="the locator cannot tell finished work from unresolved work, "
                "so every span it hands on is guesswork",
         transcripts=lambda out: [_their("mid-investigation", out)],
         command="/locate", bookmarks=False,
         check_run=_locator_check("their-mid-investigation")),
    Case("locate-recurs",
         asks="per-segment verdicts for the their-recurs fixture",
         breaks="the locator cannot tell finished work from unresolved work, "
                "so every span it hands on is guesswork",
         transcripts=lambda out: [_their("recurs", out)],
         command="/locate", bookmarks=False,
         check_run=_locator_check("their-recurs")),
    Case("locate-recurrence-a",
         asks="per-segment verdicts for the recurrence-a fixture",
         breaks="the locator cannot tell finished work from unresolved work, "
                "so every span it hands on is guesswork",
         transcripts=lambda out: [locator.FIXTURES["recurrence-a"]],
         command="/locate", bookmarks=False,
         check_run=_locator_check("recurrence-a")),
    Case("locate-tools-refine",
         asks="the same verdicts from the commands and failures alone",
         breaks="a first pass over tools only cannot tell finished work from "
                "unresolved, so the cheap stage that removes 63% of window "
                "boundaries cannot be the cheap stage",
         transcripts=lambda out: [_their("refine", out)],
         command="/locate ".strip(),
         # Failures come along at 0.8% of a corpus. Without them a command
         # list has no success signal at all -- a mutation is there and
         # nothing says whether it took -- and the first run marked a finished
         # procedure `open` for exactly that reason.
         args="--aspect tools --aspect failures",
         bookmarks=False,
         check_run=_locator_check("their-refine")),
    Case("locate-tools-mid-investigation",
         asks="the same verdicts from the commands and failures alone",
         breaks="a first pass over tools only cannot tell finished work from "
                "unresolved, so the cheap stage that removes 63% of window "
                "boundaries cannot be the cheap stage",
         transcripts=lambda out: [_their("mid-investigation", out)],
         command="/locate ".strip(),
         # Failures come along at 0.8% of a corpus. Without them a command
         # list has no success signal at all -- a mutation is there and
         # nothing says whether it took -- and the first run marked a finished
         # procedure `open` for exactly that reason.
         args="--aspect tools --aspect failures",
         bookmarks=False,
         check_run=_locator_check("their-mid-investigation")),
    Case("locate-tools-recurrence-a",
         asks="the same verdicts from the commands and failures alone",
         breaks="a first pass over tools only cannot tell finished work from "
                "unresolved, so the cheap stage that removes 63% of window "
                "boundaries cannot be the cheap stage",
         transcripts=lambda out: [locator.FIXTURES["recurrence-a"]],
         command="/locate ".strip(),
         # Failures come along at 0.8% of a corpus. Without them a command
         # list has no success signal at all -- a mutation is there and
         # nothing says whether it took -- and the first run marked a finished
         # procedure `open` for exactly that reason.
         args="--aspect tools --aspect failures",
         bookmarks=False,
         check_run=_locator_check("recurrence-a")),
    # ---- cold fixtures: shapes the prompt was never tuned against ----
    # The tuned set is fifteen segments and the prompt was revised three times
    # against it, so a clean sweep there shows mostly that it fits. Each of
    # these is a shape the tuned set does not contain: a failure that ended the
    # attempt rather than one inside finished work, work finished through a
    # project's own script with no recognisable verb, a mutation that was
    # refused so the cheap pass sees a failure present rather than absent, and
    # three segments of genuinely interleaved work.
    Case("cold-abandoned",
         asks="full content, "
              "on a fixture the prompt has not seen",
         breaks="the prompt fits the fifteen segments it was tuned on and "
                "nothing else",
         transcripts=lambda out: [_cold("cold-abandoned", out)],
         command="/locate",
         bookmarks=False,
         check_run=_cold_check("cold-abandoned")),
    Case("cold-abandoned-tools",
         asks="commands and failures only, "
              "on a fixture the prompt has not seen",
         breaks="the prompt fits the fifteen segments it was tuned on and "
                "nothing else",
         transcripts=lambda out: [_cold("cold-abandoned", out)],
         command="/locate", args="--aspect tools --aspect failures",
         bookmarks=False,
         check_run=_cold_check("cold-abandoned")),
    Case("cold-bespoke",
         asks="full content, "
              "on a fixture the prompt has not seen",
         breaks="the prompt fits the fifteen segments it was tuned on and "
                "nothing else",
         transcripts=lambda out: [_cold("cold-bespoke", out)],
         command="/locate",
         bookmarks=False,
         check_run=_cold_check("cold-bespoke")),
    Case("cold-bespoke-tools",
         asks="commands and failures only, "
              "on a fixture the prompt has not seen",
         breaks="the prompt fits the fifteen segments it was tuned on and "
                "nothing else",
         transcripts=lambda out: [_cold("cold-bespoke", out)],
         command="/locate", args="--aspect tools --aspect failures",
         bookmarks=False,
         check_run=_cold_check("cold-bespoke")),
    Case("cold-mutation-failed",
         asks="full content, "
              "on a fixture the prompt has not seen",
         breaks="the prompt fits the fifteen segments it was tuned on and "
                "nothing else",
         transcripts=lambda out: [_cold("cold-mutation-failed", out)],
         command="/locate",
         bookmarks=False,
         check_run=_cold_check("cold-mutation-failed")),
    Case("cold-mutation-failed-tools",
         asks="commands and failures only, "
              "on a fixture the prompt has not seen",
         breaks="the prompt fits the fifteen segments it was tuned on and "
                "nothing else",
         transcripts=lambda out: [_cold("cold-mutation-failed", out)],
         command="/locate", args="--aspect tools --aspect failures",
         bookmarks=False,
         check_run=_cold_check("cold-mutation-failed")),
    Case("cold-mixed",
         asks="full content, "
              "on a fixture the prompt has not seen",
         breaks="the prompt fits the fifteen segments it was tuned on and "
                "nothing else",
         transcripts=lambda out: [_cold("cold-mixed", out)],
         command="/locate",
         bookmarks=False,
         check_run=_cold_check("cold-mixed")),
    Case("cold-mixed-tools",
         asks="commands and failures only, "
              "on a fixture the prompt has not seen",
         breaks="the prompt fits the fifteen segments it was tuned on and "
                "nothing else",
         transcripts=lambda out: [_cold("cold-mixed", out)],
         command="/locate", args="--aspect tools --aspect failures",
         bookmarks=False,
         check_run=_cold_check("cold-mixed")),
    Case("discovery-fires",
         asks="a bare Python project and \"run the tests\" — nothing names the skill",
         breaks="a promoted skill is never used, and the whole pipeline "
                "produces files nothing reads",
         transcripts=lambda out: [],
         command="Run this project's tests.",
         project=_cold,
         tools="Bash(uv *),Bash(python3 *),Bash(which *),Bash(ls *),Bash(pytest *),Read,Glob,Skill",
         denials_expected=True, bookmarks=False,
         check_stream=_fired),
    Case("discovery-quiet",
         asks="the same project, but asked to write a test rather than run one",
         breaks="the description matches everything, so firing proves nothing",
         transcripts=lambda out: [],
         command="Add a test for the parse function's handling of empty input. "
                 "Do not run anything.",
         project=_cold,
         tools="Bash(uv *),Bash(python3 *),Bash(which *),Bash(ls *),Bash(pytest *),Read,Glob,Skill",
         denials_expected=True, bookmarks=False,
         check_stream=_stayed_quiet),
    Case("verify",
         asks="the promoted skill, followed literally, in a fresh trigger project",
         breaks="a promoted skill that does not work fires in every matching "
                "session from now on, and the agent trusts it",
         transcripts=lambda out: [],
         command="Run this project's tests.",
         project=_cold,
         # Bash unrestricted, in a throwaway project: the skill's subject is
         # which shell command to reach for, so an allowlist that blocks one
         # decides the result. Denials are *not* expected here -- one means
         # the case is malformed, and should say so rather than pass quietly.
         tools="Bash,Read,Glob,Skill",
         denials_expected=False, bookmarks=False,
         check_stream=_worked_as_written,
         check_run=_kept_its_promise),
    # ---- more than one procedure in a session, and the same prompt twice ----
    # Ported from the other branch's benchmark, where this branch's detection
    # was measured as non-deterministic: 2, 1, 1, 2 and 3, 2, 3, 2 over four
    # runs of identical input. Every accuracy number in either branch
    # comparison was a single run, so that spread matters more than any of them.
    #
    # The paired `-nostore` arms exist to test one hypothesis about why. Half the
    # prompt is a listing of recorded candidates, complete with their section
    # structure, immediately before the model is asked how many procedures are
    # in this session. `--auto-match` means the model no longer uses it. If it is
    # anchoring the count, removing it should tighten the spread — and if the
    # spread is unchanged, the cause is elsewhere and the next suspect is the
    # command file's own wording.
    #
    # `min_messages` is set because `two-tasks-one-sitting` is 16 messages and
    # sits below the floor. It holds two real procedures, which makes it the
    # first concrete instance of a question the handover lists as unchecked:
    # how many procedures live in sessions too short to be reviewed at all.
    Case("multi-two",
         asks="one session holding 2 unrelated finished procedures",
         breaks="two procedures collapse into one entry, or one splits into "
                "several, and the count a person is asked about is wrong",
         transcripts=lambda out: [_multitask("two-tasks-one-sitting", out)],
         min_messages=1,
         check=_counted(2)),
    Case("multi-two-nostore",
         asks="the same session with the recorded candidates withheld",
         breaks="two procedures collapse into one entry, or one splits into "
                "several, and the count a person is asked about is wrong",
         transcripts=lambda out: [_multitask("two-tasks-one-sitting", out)],
         min_messages=1,
         env={"SKILLPP_NO_STORE": "1"},
         check=_counted(2)),
    Case("multi-three",
         asks="one session holding 3 unrelated finished procedures",
         breaks="two procedures collapse into one entry, or one splits into "
                "several, and the count a person is asked about is wrong",
         transcripts=lambda out: [_multitask("three-tasks-one-morning", out)],
         min_messages=1,
         check=_counted(3)),
    Case("multi-three-nostore",
         asks="the same session with the recorded candidates withheld",
         breaks="two procedures collapse into one entry, or one splits into "
                "several, and the count a person is asked about is wrong",
         transcripts=lambda out: [_multitask("three-tasks-one-morning", out)],
         min_messages=1,
         env={"SKILLPP_NO_STORE": "1"},
         check=_counted(3)),
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


def _skills_invoked(stream: str) -> list[str]:
    """Which skills the model actually reached for.

    Read from the same event stream as the prose, and from the same tool_use
    blocks a transcript records. Nothing needs to be instrumented for this: an
    invocation is already written down, and a hook recording it separately
    would only add a second account to disagree with the first.
    """
    found = []
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{") or '"Skill"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "Skill":
                found.append(str((block.get("input") or {}).get("skill", "")))
    return found


def _spoken(stream: str) -> str:
    """Every assistant turn, not just the last one.

    ``claude -p`` prints the final message alone. A case asserting on what the
    model *offered* -- which candidates it listed before asking -- sees none of
    it, because by the last turn that is already summarised away. Streamed
    events are the only place the middle of the run survives.
    """
    out = []
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    out.append(block["text"])
        elif event.get("type") == "result" and event.get("result"):
            out.append(str(event["result"]))
    return "\n".join(out)


def run(case: Case, *, keep: bool) -> tuple[bool, str, Path, str]:
    root = Path(tempfile.mkdtemp(prefix=f"skillpp-eval-{case.name}-"))
    config = Config(root)
    if case.seed:
        case.seed(config)
    # SKILLPP_SKILLS_DIR too: the root redirects the store, but promotion
    # writes outside it, and without this a case that wrongly promotes lands a
    # SKILL.md in the developer's real ~/.claude/skills.
    env = {**os.environ, "SKILLPP_ROOT": str(root),
           "SKILLPP_SKILLS_DIR": str(root / "skills")}
    if case.min_messages is not None:
        env["SKILLPP_MIN_NEW"] = str(case.min_messages)
    env.update(case.env)
    # Cold-discovery cases run inside their own project so Claude Code
    # discovers the skill the way it would in real work.
    work = case.project(root) if case.project else REPO
    said, streams, why = [], [], ""
    for path in case.transcripts(root) or [None]:
        prompt = (f"{case.command} {path} {case.args}".strip() if path
                  else f"{case.command} {case.args}".strip())
        proc = subprocess.run(
            ["claude", "-p", prompt,
             "--no-session-persistence",
             "--output-format", "stream-json", "--verbose",
             "--allowed-tools", case.tools],
            cwd=work, env=env, capture_output=True, text=True, timeout=600)
        said.append(_spoken(proc.stdout))
        streams.append(proc.stdout)
        if proc.returncode != 0:
            # Environment failures are not judgements. An expired token, a rate
            # limit or a network error all reach the store as "nothing was
            # recorded", which is exactly what a wrong answer looks like.
            blob = (proc.stdout + proc.stderr).lower()
            for signal, plain in (
                    ("oauth", "not authenticated — run `claude` and sign in"),
                    ("401", "not authenticated — run `claude` and sign in"),
                    ("rate limit", "rate limited"),
                    ("529", "the API is overloaded"),
                    ("econnrefused", "no network")):
                if signal in blob:
                    why = f"HARNESS: {plain}, so this case did not run"
                    break
            else:
                why = f"claude exited {proc.returncode}: {proc.stderr.strip()[:400]}"
            break
        # A refused tool call reaches the store as an empty store, which is
        # indistinguishable from a judgement that found nothing. Caught here so
        # a permissions problem is never read as a model getting it wrong.
        if not case.denials_expected and any(
                w in said[-1].lower() for w in ("denied", "permission to use")):
            why = ("the model was refused a tool call, so nothing was recorded "
                   "— this is the harness, not the judgement")
            break

    # Every review bookmarks, whatever it concluded. Checked for all of them
    # rather than per case: it is the one thing a session owes the next one.
    entries = store(root)
    if not why and case.check:
        why = case.check(entries) or ""
    if not why and case.check_stream:
        why = case.check_stream("\n".join(streams)) or ""
    if not why and case.check_run:
        why = case.check_run(root, "\n".join(said), entries) or ""
    if not why and case.bookmarks:
        why = _bookmarked(root) or ""
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
