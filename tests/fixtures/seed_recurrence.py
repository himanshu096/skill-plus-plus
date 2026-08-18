#!/usr/bin/env python3
"""Two snapshots of one conversation, doing the same procedure twice.

Real sessions cannot test this. A merge only happens inside one conversation,
and one conversation repeating a whole procedure — with enough new messages
between to clear the review floor — is rare by construction. Three real runs
across related work produced no match, correctly: the procedures differed.

So this is a control-flow test, not a judgement one. The two occurrences here
are unmistakably the same work, deliberately described in different words with
a different filing target, so matching has to be done on what the procedure
*does* rather than on what it is called. If the model still records two
entries at `seen 1x`, it is not reaching for `--matches` at all.

    python3 tests/fixtures/seed_recurrence.py

Writes seeds/recurrence-a.jsonl and seeds/recurrence-b.jsonl, where b is a is a
resumed snapshot of a: same opening message, so the same conversation id, with
the second occurrence appended.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

START = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)
OUT = Path(__file__).resolve().parent


def build(counter: list[int]):
    """Record builders sharing one monotonic clock and uuid sequence."""
    def stamp() -> str:
        counter[0] += 1
        return (START + timedelta(minutes=counter[0])).isoformat().replace("+00:00", "Z")

    def user(text: str) -> dict:
        return {"type": "user", "uuid": f"u{counter[0]:03d}", "isSidechain": False,
                "timestamp": stamp(),
                "message": {"role": "user", "content": text}}

    def call(name: str, **inp) -> dict:
        return {"type": "assistant", "uuid": f"a{counter[0]:03d}", "isSidechain": False,
                "timestamp": stamp(), "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": f"t{counter[0]:03d}",
                     "name": name, "input": inp}]}}

    def result(text: str, error: bool = False) -> dict:
        block = {"type": "tool_result", "tool_use_id": f"t{counter[0]:03d}",
                 "content": text}
        if error:
            block["is_error"] = True
        return {"type": "user", "uuid": f"r{counter[0]:03d}", "isSidechain": False,
                "timestamp": stamp(), "message": {"role": "user", "content": [block]}}

    def says(text: str) -> dict:
        return {"type": "assistant", "uuid": f"s{counter[0]:03d}", "isSidechain": False,
                "timestamp": stamp(),
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": text}]}}

    return user, call, result, says


def occurrence(builders, *, ask: str, symptom: str, signature: str,
               ticket: str, component: str, thread: str,
               wrinkle: bool = False) -> list[dict]:
    """One run of the same procedure, worded differently each time.

    ``wrinkle`` adds an obstacle the other occurrence never hits, which teaches
    a rule the first run could not have known. A merged body has to carry both
    sets of rules; a body that was merely replaced will carry only the later
    one, which is the failure this distinguishes.
    """
    user, call, result, says = builders

    # The obstacle belongs where it actually bit: at the moment the component
    # was set, not before the request that started the work.
    snag: list[dict] = []
    if wrinkle:
        snag = [
            call("mcp__tracker__update_issue", issue=ticket,
                 fields=f"component={component}-svc"),
            result(f"400: 'component' must be a value from the service "
                   f"catalogue. Rejected: '{component}-svc'.", error=True),
            says("Component names are not free text — they come from the "
                 "catalogue. Looking the real one up rather than guessing."),
            call("mcp__confluence__get_page", page_id="ENG/service-catalogue"),
            result("Services: export-service | import-service | billing-api |\n"
                   "notify-worker. Use the exact string; the tracker rejects "
                   "anything else."),
        ]

    return [
        user(ask),
        says("Checking the intake spec first — the required fields changed "
             "recently and a remembered list goes stale."),
        call("mcp__confluence__get_page", page_id="ENG/bug-intake-spec"),
        result("Required: component (service catalogue), source-thread\n"
               "(permalink), severity (use the rubric, do not estimate).\n"
               "Severity: S1 data loss | S2 blocks a paid workflow |\n"
               "S3 degraded | S4 cosmetic.\n"
               "Post the ticket link back into the source thread when filed."),
        call("mcp__tracker__search_issues", jql=f"text ~ '{symptom}'"),
        result("No issues found."),
        says("Nothing on the title. Searching the stack-trace signature "
             "instead — titles get phrased differently every time."),
        call("mcp__tracker__search_issues",
             jql=f"text ~ '{signature}' ORDER BY created DESC"),
        result(f"{ticket}  {symptom}  (open, S2)"),
        says(f"{ticket} is the same signature and already open. Linking the "
             f"thread rather than filing a duplicate."),
        user("yes, link it"),
    ] + snag + [
        call("mcp__tracker__update_issue", issue=ticket,
             fields=f"source-thread={thread}, component={component}"),
        result(f"{ticket} updated."),
        call("mcp__slack__post_message", channel=thread.rsplit("/", 1)[-1],
             text=f"Tracked in {ticket}"),
        result("Posted."),
        says(f"Done — linked to {ticket}, component set, link posted back."),
    ]


def main() -> int:
    counter = [0]
    builders = build(counter)
    user, call, result, says = builders

    # Enough ordinary work around each occurrence to clear the review floor,
    # which a bare procedure would not.
    def filler(n: int, topic: str) -> list[dict]:
        out = []
        for i in range(n):
            out += [call("Bash", command=f"grep -rn '{topic}{i}' src/"),
                    result(f"src/{topic}/mod{i}.py:{40 + i}: match")]
        return out

    first = (
        [user("morning — starting on the support queue")]
        + filler(4, "export")
        + occurrence(builders,
                     ask="a customer reported the export button 500s on large "
                         "workspaces. can you file a bug",
                     symptom="Export times out on workspaces >50k rows",
                     signature="ExportSerializer.MemoryError",
                     ticket="OPS-4471", component="export-service",
                     thread="https://support.example/t/88213")
        + filler(4, "queue")
    )

    # Same procedure, different words, different target. Nothing names it the
    # same way; only the shape matches.
    second = (
        [user("another one came in from support")]
        + filler(4, "import")
        + occurrence(builders,
                     ask="someone says the CSV import dies halfway through on "
                         "big files. log a defect for it please",
                     symptom="Import aborts partway on large uploads",
                     signature="ImportStreamer.ChunkOverrun",
                     ticket="OPS-5120", component="import-service",
                     thread="https://support.example/t/91004",
                     wrinkle=True)
        + filler(4, "retry")
    )

    OUT.mkdir(parents=True, exist_ok=True)
    a, b = OUT / "recurrence-a.jsonl", OUT / "recurrence-b.jsonl"
    a.write_text("".join(json.dumps(r) + "\n" for r in first), encoding="utf-8")
    b.write_text("".join(json.dumps(r) + "\n" for r in first + second),
                 encoding="utf-8")

    print(f"wrote {a}  ({len(first)} records)")
    print(f"wrote {b}  ({len(first + second)} records — a resumed snapshot of a)")
    print("\nreview a, then b. b's increment is the second occurrence only.")
    print()
    print("The second occurrence hits an obstacle the first never did: the")
    print("tracker rejects a guessed component name, so it has to be looked up")
    print("in the service catalogue. A merged body carries that rule *and*")
    print("everything the first run established.")
    print()
    print("pass  -> one entry, seen 2x, body has the signature-search and")
    print("         link-do-not-duplicate rules AND the catalogue rule")
    print("fail  -> body has only the catalogue rule (replaced, not merged)")
    print("      -> or two entries at seen 1x (never matched at all)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
