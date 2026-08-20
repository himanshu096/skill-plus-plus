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
    check_run: Callable[[Path, str, list[dict]], str | None] | None = None


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
         asks="the same verdicts from the tool calls alone, no narration",
         breaks="a first pass over tools only cannot tell finished work from "
                "unresolved, so the cheap stage that removes 63% of window "
                "boundaries cannot be the cheap stage",
         transcripts=lambda out: [_their("refine", out)],
         command="/locate ".strip(),
         args="--aspect tools",
         bookmarks=False,
         check_run=_locator_check("their-refine")),
    Case("locate-tools-mid-investigation",
         asks="the same verdicts from the tool calls alone, no narration",
         breaks="a first pass over tools only cannot tell finished work from "
                "unresolved, so the cheap stage that removes 63% of window "
                "boundaries cannot be the cheap stage",
         transcripts=lambda out: [_their("mid-investigation", out)],
         command="/locate ".strip(),
         args="--aspect tools",
         bookmarks=False,
         check_run=_locator_check("their-mid-investigation")),
    Case("locate-tools-recurrence-a",
         asks="the same verdicts from the tool calls alone, no narration",
         breaks="a first pass over tools only cannot tell finished work from "
                "unresolved, so the cheap stage that removes 63% of window "
                "boundaries cannot be the cheap stage",
         transcripts=lambda out: [locator.FIXTURES["recurrence-a"]],
         command="/locate ".strip(),
         args="--aspect tools",
         bookmarks=False,
         check_run=_locator_check("recurrence-a")),
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
    said, why = [], ""
    for path in case.transcripts(root) or [None]:
        prompt = (f"{case.command} {path} {case.args}".strip() if path
                  else f"{case.command} {case.args}".strip())
        proc = subprocess.run(
            ["claude", "-p", prompt,
             "--no-session-persistence",
             "--output-format", "stream-json", "--verbose",
             "--allowed-tools", case.tools],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=600)
        said.append(_spoken(proc.stdout))
        if proc.returncode != 0:
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
