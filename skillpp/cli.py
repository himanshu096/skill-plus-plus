"""Command line interface.

Division of labour: this CLI does everything deterministic. The
``/skillpp-review`` slash command drives an agent through the parts that need
judgement — reading the proposal, resolving what the repo can answer, asking
the developer at most three questions, and writing the final prose.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .capture import (fold_dictation, handle_prompt, handle_session_end,
                      handle_tool, log_error)
from .config import Config, default_skills_dir
from .ledger import (IGNORED_STATUSES, Ledger, STATUS_CANDIDATE,
                     STATUS_IGNORED, STATUS_PROMOTED)
from .lifecycle import move_tier, scan
from .signals import detect
from .summary import (check_dependencies, questions_for, render_proposal,
                      scaffold_skill)


# --------------------------------------------------------------------------
# hook — must never fail loudly
# --------------------------------------------------------------------------

def cmd_hook(args: argparse.Namespace) -> int:
    """Read a hook payload on stdin and record it.

    Always exits 0. A capture failure must never disrupt the developer's
    session; errors go to the log instead.
    """
    config = Config(args.root)
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        event = args.event or payload.get("hook_event_name", "")
        config.ensure_dirs()

        if event == "UserPromptSubmit":
            handle_prompt(config, payload)
        elif event == "PostToolUse":
            handle_tool(config, payload)
        elif event in ("SessionEnd", "Stop"):
            result = handle_session_end(config, payload)
            if args.verbose:
                print(json.dumps(result))
        elif args.verbose:
            print(json.dumps({"status": "ignored-event", "event": event}))
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all
        log_error(config, f"hook error: {type(exc).__name__}: {exc}")
    return 0


# --------------------------------------------------------------------------
# session review
# --------------------------------------------------------------------------

def cmd_prepare_session(args: argparse.Namespace) -> int:
    """Print a session's new work, ready to drop into a prompt.

    Exits 0 even when there is nothing to review. The caller substitutes this
    output into a skill via `` !`command` ``, where a status code is invisible
    — so "stop, there is nothing here" has to be a sentence the model reads.

    ``--window`` narrows the output to one window, which is opt-in on purpose.
    Whole-session rendering is what the 18-of-18 eval arm measures, and real
    sessions render at 60k-128k tokens against a largest tested case of 16k. The
    flag exists so that gap can be A/B'd rather than closed by assumption.
    """
    from .prepare import TranscriptNotFound, prepare, render

    config = Config(args.root)
    try:
        prepared = prepare(args.target, config=config)
    except (TranscriptNotFound, ValueError) as exc:
        print(f"Could not prepare this session: {exc}\n\n"
              f"Stop here and write nothing.")
        return 0

    if args.window is None and not args.windows:
        print(render(prepared))
        return 0

    from .window import windows
    panes = windows(prepared.new_messages)
    if not panes:
        print("Nothing new in this session. Stop here and write nothing.")
        return 0

    if args.windows:
        print(f"{len(panes)} window(s) over {len(prepared.new_messages)} "
              f"new messages:\n")
        for i, pane in enumerate(panes):
            first, last = pane.span
            print(f"  {i}  requests {first}-{last}  {pane.tokens:>6,} tokens"
                  + (f"  ({pane.overlapping} carried from the window before)"
                     if pane.overlapping else ""))
        return 0

    if not 0 <= args.window < len(panes):
        print(f"No window {args.window} — this session has {len(panes)} "
              f"(0-{len(panes) - 1}).\n\nStop here and write nothing.")
        return 0

    pane = panes[args.window]
    first, last = pane.span
    print(f"window         {args.window} of {len(panes)}")
    print(f"requests       {first}-{last}")
    if pane.overlapping:
        # Said out loud because a caller comparing findings across windows has
        # to know which ones it has already been shown.
        print(f"carried over   the first {pane.overlapping} request(s) were also "
              f"in the previous window")
    print()
    print(pane.text)
    return 0


def cmd_record_candidate(args: argparse.Namespace) -> int:
    """Fold one proposed skill into the conversation's memory.

    The model decides only whether this is new or matches something already
    recorded. Counting, provenance, ordering and rewriting happen in code,
    where they cannot be forgotten.
    """
    from .prepare import TranscriptNotFound, prepare, record

    config = Config(args.root)
    body = sys.stdin.read().strip()
    if not body:
        print("nothing on stdin; a candidate needs a body", file=sys.stderr)
        return 1
    try:
        prepared = prepare(args.target, config=config)
        written = record(prepared, name=args.name, body=body,
                         matches=args.matches)
    except (TranscriptNotFound, ValueError, KeyError) as exc:
        print(str(exc).strip("'"), file=sys.stderr)
        return 1
    verb = f"merged into '{args.matches}'" if args.matches else "recorded"
    print(f"{verb}: {args.name} -> {written}")
    return 0


def cmd_promote_candidate(args: argparse.Namespace) -> int:
    """Turn a candidate into a skill on disk, and record that it happened.

    Writes the SKILL.md first, then appends the decision. In that order: a log
    claiming a skill exists when the file was never written is worse than a
    file with no record, because nothing will ever propose the procedure again.
    """
    from .memory import PROMOTED, find, load, record_decision

    config = Config(args.root)
    entry = find(load(config), args.name)
    if entry is None:
        print(f"No candidate named '{args.name}'. See: skillpp candidates",
              file=sys.stderr)
        return 1

    skills_dir = Path(args.skills_dir).expanduser() if args.skills_dir \
        else default_skills_dir()
    path = skills_dir / entry.name / "SKILL.md"
    if path.exists() and not args.force:
        print(f"{path} already exists; pass --force to overwrite", file=sys.stderr)
        return 1

    # The description decides whether the skill is ever found: it is what
    # Claude reads when choosing among many. Derived from the body only as a
    # starting point, and said out loud so a poor one gets noticed.
    description = args.description or entry.body.strip().split("\n")[0][:200]
    path.parent.mkdir(parents=True, exist_ok=True)
    front = [
        "---",
        f"name: {entry.name}",
        f"description: {json.dumps(description, ensure_ascii=False)}",
    ]
    # A dedicated field that is appended to the description in the skill
    # listing, so trigger phrasing reaches the one place that decides whether
    # the skill fires -- before the body has loaded at all.
    if args.when_to_use:
        front.append(f"when_to_use: {json.dumps(args.when_to_use, ensure_ascii=False)}")
    path.write_text("\n".join(front + [
        "metadata:",
        '  source: "skill-plus-plus"',
        f'  seen: {entry.count}',
        f'  sessions: {json.dumps(entry.sessions)}',
        "---",
        "",
        entry.body.strip(),
        "",
    ]), encoding="utf-8")

    # Appended after the file exists, never before: a decision claiming a skill
    # is on disk when it is not stops the procedure ever being proposed again.
    record_decision(config, name=entry.name, action=PROMOTED,
                    skill_path=str(path))

    print(f"wrote {path}")
    print(f"description: {description}")
    print("Review that description — it is what decides when the skill fires.")
    return 0


def cmd_segments(args: argparse.Namespace) -> int:
    """Print a session's segments, numbered, one request and its work each.

    The input to the locator question, which is asked per segment rather than
    per session: did the work in this one reach a resolved state? That is a
    visible fact about a small piece of text, which is the shape a small model
    answers reliably -- and it is deliberately *not* "is this worth keeping",
    which needs the whole span in working memory.

    Exits 0 with a sentence when there is nothing, for the same reason
    ``prepare-session`` does: this output is substituted into a prompt where an
    exit code cannot be seen.
    """
    from .prepare import TranscriptNotFound, resolve
    from .transcript import read
    from .window import ASPECTS, DEFAULT_ASPECTS, keep_aspects, segments

    try:
        path = resolve(args.target)
        segs = segments(read(path))
    except (TranscriptNotFound, ValueError, OSError) as exc:
        print(f"Could not read this session: {exc}\n\n"
              f"Stop here and write nothing.")
        return 0
    if not segs:
        print("This session has no requests in it. Stop here and write nothing.")
        return 0

    if args.only is not None:
        # One segment, for a caller asking one question about it. The index is
        # the same one every other reader uses, so an answer about segment 4 is
        # about segment 4 wherever it came from.
        wanted = [s for s in segs if s.index == args.only]
        if not wanted:
            print(f"This session has no segment {args.only} — it has "
                  f"{len(segs)} ({0}-{len(segs) - 1}).\n\n"
                  f"Stop here and write nothing.")
            return 0
        only_of = len(segs)
        # What the developer said next, as context and nothing more. Whether a
        # request resolved is partly answered by what followed it: a new subject
        # says they moved on, more of the same says they did not. A frontier
        # model reading one segment in isolation marked a finished procedure
        # `open` for want of exactly this, while the all-at-once prompt — which
        # sees the neighbours — got it right.
        following = next((s for s in segs if s.index == args.only + 1), None)
        next_ask = following.ask if following else ""
        segs = wanted

    chosen = tuple(args.aspect) if args.aspect else DEFAULT_ASPECTS
    unknown = [a for a in chosen if a not in ASPECTS.values()]
    if unknown:
        print(f"Unknown aspect(s) {unknown}. Choose from "
              f"{sorted(set(ASPECTS.values()))}.\n\nStop here and write nothing.")
        return 0

    print(f"transcript     {path.name}")
    if args.only is not None:
        # Not "segments 1": a caller reading that would think the session has
        # one request, and this output goes straight into a prompt.
        print(f"segment        {args.only} of {only_of}")
    else:
        print(f"segments       {len(segs)}")
    if chosen != DEFAULT_ASPECTS:
        print(f"showing        {', '.join(chosen)} only — the rest of each "
              f"request is deliberately withheld")
    print()
    for seg in segs:
        text = (seg.text if chosen == DEFAULT_ASPECTS
                else keep_aspects(seg.text, chosen))
        print(f"--- segment {seg.index} ---")
        # An empty segment is still numbered. Renumbering to close a gap would
        # break the correspondence the answers and `reconcile` both rely on.
        print(text if text.strip() else "(nothing of the shown aspects here)")
        print()
    if args.only is not None:
        print("--- what the developer said next (context, not what you are "
              "judging) ---")
        print(f"> {next_ask}" if next_ask
              else "(nothing — this was the last request in the session)")
        print()
    return 0


def cmd_locate(args: argparse.Namespace) -> int:
    """Print which requests in a session landed, and the spans between them.

    The cheap half of detection, run locally. What it produces is a shortlist of
    stretches worth an expensive read — not a verdict about any skill.

    Deliberately prints its own uncertainty: a request the models disagreed
    about is marked, and stays inside a span rather than ending one.
    """
    from .local import LocalModelUnavailable, LENIENT, STRICT, spans, verdicts
    from .prepare import TranscriptNotFound, resolve
    from .transcript import read
    from .window import DEFAULT_ASPECTS, keep_aspects, segments

    try:
        path = resolve(args.target)
        segs = segments(read(path))
    except (TranscriptNotFound, ValueError, OSError) as exc:
        print(f"Could not read this session: {exc}", file=sys.stderr)
        return 1
    if not segs:
        print("No requests in this session.")
        return 0

    models = tuple(args.model) if args.model else (
        (STRICT, LENIENT) if args.pair else (STRICT,))
    chosen = tuple(args.aspect) if args.aspect else DEFAULT_ASPECTS
    render = (lambda seg: keep_aspects(seg.text, chosen)
              if chosen != DEFAULT_ASPECTS else seg.text)

    try:
        found = verdicts(segs, models=models, render=render)
    except LocalModelUnavailable as exc:
        print(f"{exc}\n\nInstall the model with: ollama pull {models[0]}",
              file=sys.stderr)
        return 1

    print(f"{path.name}: {len(segs)} requests, {', '.join(models)}\n")
    by_index = {s.index: s for s in segs}
    for verdict in found:
        mark = "landed" if verdict.landed else (
            "?" if verdict.escalates else "open")
        print(f"  {verdict.index:>3}  {mark:<7} {verdict.why:<34} "
              f"{by_index[verdict.index].ask[:44]!r}")

    found_spans = spans(found)
    unsure = [v.index for v in found if v.escalates]
    print(f"\n{len(found_spans)} span(s) for a judge to read: {found_spans}")
    if unsure:
        print(f"{len(unsure)} request(s) the models disagreed on: {unsure} — "
              f"kept inside their spans, not treated as endings")
    if not found_spans:
        print("Nothing landed, so nothing to read. Most sessions are like this.")
    return 0


def cmd_drain(args: argparse.Namespace) -> int:
    """Review the sessions that ended while nobody was looking.

    The `SessionEnd` hook appends every ended session to a queue and nothing
    has ever emptied it: measured here, 159 queued and 157 unreviewed. That is
    the gap between "watches how work gets done" and "waits to be asked".

    Two things this has to survive. A queued transcript can be gone by the time
    anyone looks — 143 of those 157 were — so a missing file is skipped and
    counted rather than treated as an error. And a drain that spawns
    `claude -p` without `--no-session-persistence` creates a session that ends,
    which the hook enqueues, which the next drain picks up: the queue refills
    itself faster than it empties.

    Dry run unless ``--apply``, because each application is a model call.
    """
    import subprocess
    from .prepare import project_slug

    config = Config(args.root)
    queue = Path(args.queue).expanduser() if args.queue else (
        Path.cwd() / ".claude" / "skillpp" / "memory" / "ended-sessions.jsonl")
    if not queue.is_file():
        print(f"No queue at {queue}. Nothing has ended yet, or the SessionEnd "
              f"hook is not installed.")
        return 0

    reviewed = {p.stem for p in config.reviews_dir.glob("*.md")}
    seen: set[str] = set()
    todo: list[tuple[str, Path]] = []
    gone = done = 0
    for line in queue.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        session = str(row.get("session_id") or "")
        if not session or session in seen:
            continue
        seen.add(session)
        if session in reviewed:
            done += 1
            continue
        path = Path(str(row.get("transcript_path") or "")).expanduser()
        if not path.is_file():
            gone += 1
            continue
        todo.append((session, path))

    print(f"queue          {queue}")
    print(f"ended          {len(seen)} sessions")
    print(f"reviewed       {done}")
    print(f"transcript gone {gone}")
    print(f"to review      {len(todo)}")
    if args.prune and gone:
        kept = []
        for line in queue.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = Path(str(row.get("transcript_path") or "")).expanduser()
            if path.is_file():
                kept.append(line)
        queue.write_text("\n".join(kept) + ("\n" if kept else ""),
                         encoding="utf-8")
        print(f"pruned         {gone} entries with no transcript to review")

    if args.limit:
        todo = todo[: args.limit]
        print(f"limited to     {len(todo)} this run")
    if not todo:
        return 0

    if not args.apply:
        print("\nDry run. Each of these is one model call:\n")
        for session, path in todo:
            print(f"  claude -p '/log-session {path}' "
                  f"--no-session-persistence")
        print("\nRun again with --apply to spend them.")
        return 0

    for n, (session, path) in enumerate(todo, 1):
        print(f"\n[{n}/{len(todo)}] {session[:8]} {path.name}")
        done_proc = subprocess.run(
            ["claude", "-p", f"/log-session {path}",
             # Mandatory: without it this session ends, the hook enqueues it,
             # and the queue refills itself.
             "--no-session-persistence",
             "--allowed-tools", "Bash(python3 bin/skillpp *)"],
            capture_output=True, text=True, timeout=900)
        out = (done_proc.stdout or done_proc.stderr).strip()
        print("".join(f"    {ln}\n" for ln in out.splitlines()[-6:]))
    return 0


def cmd_candidates(args: argparse.Namespace) -> int:
    """List what has been recorded, most-seen first."""
    from .memory import CANDIDATE, PROMOTED, THRESHOLD, load, reviewable

    config = Config(args.root)
    store = config.patterns_dir
    entries = load(config)
    if not entries:
        print(f"Nothing recorded yet. Review a session with /log-session.\n"
              f"Store: {store}")
        return 0

    ready = reviewable(entries)
    # --open-only feeds the review queue, so it is the threshold that applies
    # rather than the status alone. The plain listing below still shows
    # everything: what has been seen once is a log, and a log is worth reading.
    if args.open_only:
        entries = ready
    if args.json:
        print(json.dumps([{
            "name": e.name, "status": e.status, "count": e.count,
            "sessions": e.sessions,
            "first_seen": e.first_seen, "last_seen": e.last_seen,
            "skill_path": e.skill_path, "body": e.body,
        } for e in entries], indent=2))
        return 0

    logged = [e for e in entries
              if e.status == CANDIDATE and e.count < THRESHOLD]
    done = [e for e in entries if e.status == PROMOTED]
    print(f"{len(ready)} ready to review, {len(logged)} still logging, "
          f"{len(done)} promoted — {store}\n")
    for entry in entries:
        if entry.status == PROMOTED:
            mark = "✓ "
        elif entry.count >= THRESHOLD:
            mark = "▸ "
        else:
            mark = "  "
        print(f"{mark}x{entry.count}  {entry.name}")
        if entry.skill_path:
            print(f"        {entry.skill_path}")
        # Not truncated. Shortening a session id for display is what once hid
        # a collision that froze a count at 1 with no error.
        print(f"        {', '.join(entry.sessions)}")
    print(f"\n▸  seen {THRESHOLD}× or more — /review-candidates asks about these.")
    print(f"Read one in full:  less {store}/<name>.md")
    return 0


def cmd_commit_session(args: argparse.Namespace) -> int:
    """Declare the session reviewed, advancing the watermark.

    Separate from recording candidates: a session may yield three or none, and
    the watermark should move exactly once either way.
    """
    from .prepare import TranscriptNotFound, commit, prepare

    config = Config(args.root)
    try:
        prepared = prepare(args.target, config=config)
    except (TranscriptNotFound, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not prepared.new_messages:
        print("nothing new in this session; watermark unchanged")
        return 0
    print(f"reviewed up to {prepared.watermark[0][:8]} -> {commit(prepared)}")
    return 0


# --------------------------------------------------------------------------
# ledger inspection
# --------------------------------------------------------------------------

def cmd_review(args: argparse.Namespace) -> int:
    """List ledger candidates ready for review."""
    config = Config(args.root)
    ledger = Ledger(config)
    entries = ledger.candidates(ready_only=not args.all)
    if args.json:
        print(json.dumps([{
            "id": e.id, "title": e.title, "occurrences": e.occurrences,
            "steps": len(e.steps), "last_seen": e.last_seen,
            "questions": len(questions_for(e, config)),
            "deps_cli": e.deps_cli, "deps_mcp": e.deps_mcp,
        } for e in entries], indent=2))
        return 0
    if not entries:
        scope = "candidates" if args.all else f"candidates at {config.recurrence_threshold}+ occurrences"
        print(f"No {scope} in the ledger.")
        return 0
    print(f"{len(entries)} candidate(s) ready for review:\n")
    for entry in entries:
        n_q = len(questions_for(entry, config))
        flag = f"  {n_q} question(s)" if n_q else "  no open questions"
        print(f"  {entry.id}  ×{entry.occurrences}  {entry.title[:58]}")
        print(f"            {len(entry.steps)} steps ·{flag} · last seen {entry.last_seen[:10]}")
    print(f"\nInspect one:  skillpp show <id>")
    return 0


def cmd_dictate(args: argparse.Namespace) -> int:
    """Create a candidate from a description instead of an observed trace."""
    config = Config(args.root)
    config.ensure_dirs()
    text = args.text if args.text else sys.stdin.read()
    result = fold_dictation(config, text, args.title or "")
    if result["status"] == "empty":
        print("Nothing to work with — describe the workflow in a sentence or two.",
              file=sys.stderr)
        return 1
    entry = Ledger(config).get(result["id"])
    if args.json:
        print(json.dumps({
            "id": entry.id, "status": result["status"], "title": entry.title,
            "steps": [s["input"]["text"] for s in entry.steps],
            "questions": [q.to_dict() for q in questions_for(entry, config)],
            "total_questions": len(detect(entry)),
        }, indent=2))
        return 0
    if result["status"] == "merged":
        print(f"That matches an existing dictated candidate: {entry.id}\n")
    print(render_proposal(entry, config))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Render a candidate's proposal, evidence and open questions."""
    config = Config(args.root)
    entry = Ledger(config).get(args.id)
    if not entry:
        print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({
            "id": entry.id, "title": entry.title, "occurrences": entry.occurrences,
            "intents": entry.intents, "steps": entry.steps,
            "deps_cli": entry.deps_cli, "deps_mcp": entry.deps_mcp,
            "questions": [q.to_dict() for q in questions_for(entry, config)],
        }, indent=2))
        return 0
    print(render_proposal(entry, config))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Search the ledger for entries matching the query."""
    config = Config(args.root)
    results = Ledger(config).search(" ".join(args.query))
    if not results:
        print("Nothing in the ledger matches that.")
        return 0
    for score, entry in results[: args.limit]:
        print(f"  {entry.id}  ×{entry.occurrences}  [{score:.2f}]  {entry.title[:60]}")
        print(f"            last seen {entry.last_seen[:10]} · {len(entry.steps)} steps")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Print ledger size and status counts."""
    config = Config(args.root)
    stats = Ledger(config).stats()
    print(f"ledger      {config.ledger_dir}")
    print(f"entries     {stats['total']}")
    print(f"  candidate {stats['candidates']}  (ready: {stats['ready']})")
    print(f"  promoted  {stats['promoted']}")
    print(f"  dismissed {stats['dismissed']}")
    print(f"size        {stats['bytes'] / 1024:.1f} KB")
    return 0


# --------------------------------------------------------------------------
# promotion
# --------------------------------------------------------------------------

def cmd_scaffold(args: argparse.Namespace) -> int:
    """Generate a starting SKILL.md for a candidate."""
    config = Config(args.root)
    entry = Ledger(config).get(args.id)
    if not entry:
        print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
        return 1
    answers = json.loads(args.answers) if args.answers else {}
    text = scaffold_skill(entry, args.name, args.description, answers, args.tier)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(text)
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    """Mark a candidate promoted. The SKILL.md itself is written by the agent."""
    config = Config(args.root)
    ledger = Ledger(config)
    entry = ledger.get(args.id)
    if not entry:
        print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
        return 1
    skill_path = Path(args.skill_path).expanduser() if args.skill_path else None
    if skill_path and not skill_path.exists():
        print(f"Skill file does not exist: {skill_path}", file=sys.stderr)
        return 1
    entry.status = STATUS_PROMOTED
    entry.skill_path = str(skill_path) if skill_path else ""
    entry.promoted_at = datetime.now(timezone.utc).replace(
        microsecond=0).isoformat()
    if args.note:
        entry.notes = args.note
    ledger.save(entry)
    print(f"promoted {entry.id}" + (f" → {skill_path}" if skill_path else ""))
    return 0


def cmd_ignore(args: argparse.Namespace) -> int:
    """Park a workflow so it is never proposed again."""
    config = Config(args.root)
    ledger = Ledger(config)
    entry = ledger.get(args.id)
    if not entry:
        print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
        return 1
    was = entry.status
    entry.status = STATUS_IGNORED
    entry.ignored_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    entry.ignored_at_occurrences = entry.occurrences
    if args.note:
        entry.notes = args.note
    ledger.save(entry)
    print(f"ignored {entry.id} (was {was}) — {entry.title[:50]}")
    print("Reverse with:  skillpp reopen " + entry.id)
    return 0


def cmd_ignored(args: argparse.Namespace) -> int:
    """List the ignore set — the category is only useful if it is visible."""
    config = Config(args.root)
    entries = [e for e in Ledger(config).all() if e.status in IGNORED_STATUSES]
    threshold = config.recurrence_threshold
    if args.json:
        print(json.dumps([{
            "id": e.id, "title": e.title, "notes": e.notes,
            "occurrences": e.occurrences, "ignored_at": e.ignored_at,
            "recurrences_since_ignored": e.recurrences_since_ignored,
            "ignore_looks_wrong": e.ignore_looks_wrong(threshold),
        } for e in entries], indent=2))
        return 0
    if not entries:
        print("Nothing ignored.")
        return 0

    print(f"{len(entries)} ignored workflow(s) — never proposed:\n")
    nagging = []
    for entry in entries:
        since = entry.recurrences_since_ignored
        mark = "  ⚠" if entry.ignore_looks_wrong(threshold) else ""
        print(f"  {entry.id}  ×{entry.occurrences}  {entry.title[:52]}{mark}")
        if entry.ignored_at:
            tail = f", {since}× since" if since else ""
            print(f"            ignored {entry.ignored_at[:10]}{tail}")
        if entry.notes:
            print(f"            {entry.notes[:70]}")
        if entry.ignore_looks_wrong(threshold):
            nagging.append(entry)

    if nagging:
        print(f"\n⚠ {len(nagging)} of these have recurred {threshold}+ times since "
              f"you ignored them.\n  You keep doing the work — worth a second look:")
        for entry in nagging:
            print(f"    skillpp reopen {entry.id}   {entry.title[:44]}")
    print("\nBring one back:  skillpp reopen <id>")
    return 0


def cmd_reopen(args: argparse.Namespace) -> int:
    """Return a workflow to the review queue, from ignored or promoted."""
    config = Config(args.root)
    ledger = Ledger(config)
    entry = ledger.get(args.id)
    if not entry:
        print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
        return 1
    was = entry.status
    if was == STATUS_CANDIDATE:
        print(f"{entry.id} is already a candidate")
        return 0
    entry.status = STATUS_CANDIDATE
    entry.skill_path = ""
    entry.notes = args.note or f"Reopened from {was}."
    ledger.save(entry)
    ready = entry.ready(config.recurrence_threshold)
    print(f"reopened {entry.id} (was {was}) — {entry.title[:50]}")
    print("  surfaces in `skillpp review` now" if ready
          else f"  at {entry.occurrences} occurrence(s); needs "
               f"{config.recurrence_threshold} to surface")
    return 0


def cmd_expire(args: argparse.Namespace) -> int:
    """Delete unapproved candidates past their TTL."""
    config = Config(args.root)
    removed = Ledger(config).expire()
    print(f"expired {len(removed)} unapproved candidate(s) older than "
          f"{config.candidate_ttl_days} days")
    for entry_id in removed:
        print(f"  {entry_id}")
    return 0


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

def cmd_lifecycle(args: argparse.Namespace) -> int:
    """Inventory skills with tier and staleness marks."""
    config = Config(args.root)
    skills_dir = Path(args.skills_dir).expanduser() if args.skills_dir else default_skills_dir()
    project_root = Path(args.project_root).expanduser() if args.project_root else Path.cwd()
    skills = scan(skills_dir, config, project_root)
    if not skills:
        print(f"No skills found under {skills_dir}")
        return 0
    print(f"{len(skills)} skill(s) · hot dir {skills_dir}\n")
    for skill in skills:
        marks = []
        if skill.is_stale:
            marks.append(f"STALE({len(skill.stale_refs)})")
        if skill.uses == 0:
            marks.append("never used")
        suffix = ("  " + " ".join(marks)) if marks else ""
        print(f"  [{skill.tier:8}] {skill.name:32} uses={skill.uses}{suffix}")
        if args.verbose and skill.stale_refs:
            for ref in skill.stale_refs:
                print(f"                 ↳ unresolved {ref}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Match promoted ledger entries against the skills actually on disk."""
    from .lifecycle import reconcile

    config = Config(args.root)
    skills_dir = Path(args.skills_dir).expanduser() if args.skills_dir else default_skills_dir()
    result = reconcile(Ledger(config), skills_dir, config)

    print(f"{len(result['ok'])} promoted skill(s) present and accounted for")

    if result["missing"]:
        verb = "moved to the ignore list" if args.apply else "would move to the ignore list"
        print(f"\n{len(result['missing'])} promoted skill(s) no longer on disk — {verb}:")
        for entry in result["missing"]:
            when = f" (promoted {entry.promoted_at[:10]})" if entry.promoted_at else ""
            print(f"\n  {entry.id}  {entry.title[:52]}{when}")
            print(f"            gone: {entry.skill_path}")
            if args.apply:
                entry.status = STATUS_IGNORED
                entry.ignored_at = datetime.now(timezone.utc).replace(
                    microsecond=0).isoformat()
                entry.ignored_at_occurrences = entry.occurrences
                entry.notes = (
                    f"Skill deleted (was {entry.skill_path}). Parked here rather "
                    f"than re-proposed. If the workflow keeps recurring, "
                    f"`skillpp ignored` will flag it."
                )
                entry.skill_path = ""
                Ledger(config).save(entry)
            else:
                print(f"            skillpp reopen {entry.id}   → propose it again")

    if result["unlinked"]:
        print(f"\n{len(result['unlinked'])} promoted without a recorded path "
              f"(cannot verify):")
        for entry in result["unlinked"]:
            print(f"  {entry.id}  {entry.title[:60]}")

    if result["orphaned"]:
        print(f"\n{len(result['orphaned'])} skill(s) authored here whose ledger "
              f"entry is gone (harmless — provenance only):")
        for skill in result["orphaned"]:
            print(f"  {skill.name}  ({skill.provenance})")

    return 0


def cmd_tier(args: argparse.Namespace) -> int:
    """Move a skill between hot/cold/archived tiers."""
    config = Config(args.root)
    skills_dir = Path(args.skills_dir).expanduser() if args.skills_dir else default_skills_dir()
    matches = [s for s in scan(skills_dir, config) if s.name == args.name]
    if not matches:
        print(f"No skill named '{args.name}'", file=sys.stderr)
        return 1
    dest = move_tier(matches[0], args.tier, skills_dir, config)
    print(f"{args.name}: {matches[0].tier} → {args.tier}  ({dest})")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Dependency check at pull time."""
    config = Config(args.root)
    if args.id:
        entry = Ledger(config).get(args.id)
        if not entry:
            print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
            return 1
        deps_cli, deps_mcp, label = entry.deps_cli, entry.deps_mcp, entry.id
    else:
        skills_dir = Path(args.skills_dir).expanduser() if args.skills_dir else default_skills_dir()
        matches = [s for s in scan(skills_dir, config) if s.name == args.name]
        if not matches:
            print(f"No skill named '{args.name}'", file=sys.stderr)
            return 1
        deps_cli, deps_mcp, label = matches[0].requires_cli, matches[0].requires_mcp, args.name

    result = check_dependencies(deps_cli, deps_mcp, Path.cwd())
    if result["ok"]:
        print(f"{label}: all dependencies present")
        return 0
    print(f"{label}: MISSING DEPENDENCIES")
    for dep in result["missing_cli"]:
        print(f"  cli  {dep}  (not on PATH)")
    for dep in result["missing_mcp"]:
        print(f"  mcp  {dep}  (server not configured)")
    print("\nThis skill will not run here. Install the missing dependencies "
          "rather than working around them.")
    return 2


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------

def cmd_bundle(args: argparse.Namespace) -> int:
    """Package skills as a distributable Claude Code plugin."""
    from .install import build_plugin_bundle

    config = Config(args.root)
    skills_dir = Path(args.skills_dir).expanduser() if args.skills_dir else default_skills_dir()
    found = scan(skills_dir, config)
    if args.name_filter:
        found = [s for s in found if s.name in args.name_filter]
    found = [s for s in found if s.tier == "hot" or args.include_cold]
    if not found:
        print(f"No skills to bundle under {skills_dir}", file=sys.stderr)
        return 1

    out = Path(args.out).expanduser()

    if args.format == "upload":
        from .install import build_upload_bundle, validate_for_upload
        failed = False
        for skill in found:
            problems = validate_for_upload(skill.path)
            if problems:
                failed = True
                print(f"✗ {skill.name}")
                for problem in problems:
                    print(f"    {problem}")
                continue
            archive = build_upload_bundle(skill.path, out)
            print(f"✓ {skill.name}  →  {archive}")
        if failed:
            print("\nFix the problems above and re-run. Upload via "
                  "Customize → Skills.", file=sys.stderr)
            return 1
        print("\nUpload via Customize → Skills. One zip per skill.")
        return 0

    commands = []
    if args.with_commands:
        commands_dir = Path(__file__).resolve().parent.parent / "commands"
        commands = sorted(commands_dir.glob("*.md"))

    result = build_plugin_bundle(
        [s.path for s in found], out, args.plugin_name, args.description,
        args.plugin_version, commands)

    print(f"plugin   {args.plugin_name} v{args.plugin_version}")
    print(f"root     {result['root']}")
    print(f"skills   {', '.join(result['skills'])}")
    if result["commands"]:
        print(f"commands {', '.join(result['commands'])}")

    if args.zip:
        archive = shutil.make_archive(str(out), "zip", root_dir=str(out))
        print(f"archive  {archive}")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Wire Claude Code hooks into settings.json (dry run by default)."""
    from .install import apply_settings, hook_command, install_command_file, plan_settings

    settings_path = Path(args.settings).expanduser() if args.settings else (
        Path.home() / ".claude" / "settings.json")
    try:
        merged, changes = plan_settings(settings_path)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"settings file : {settings_path}")
    print(f"hook command  : {hook_command()}")
    print("planned changes:")
    for change in changes:
        print(f"  - {change}")

    if not args.apply:
        print("\nDry run. Nothing was written.")
        print("Re-run with --apply to install, or copy the hooks block below "
              "into your settings manually:\n")
        print(json.dumps({"hooks": merged.get("hooks", {})}, indent=2))
        return 0

    backup = apply_settings(settings_path, merged)
    print(f"\nwrote {settings_path}" + (f" (backup: {backup})" if backup else ""))
    commands_dir = Path(args.commands_dir).expanduser() if args.commands_dir else (
        Path.cwd() / ".claude" / "commands")
    try:
        dest = install_command_file(commands_dir)
        print(f"wrote {dest}")
    except OSError as exc:
        print(f"could not install slash command: {exc}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillpp",
        description="Skill Plus Plus — capture workflows passively, promote them deliberately.")
    parser.add_argument("--version", action="version", version=f"skillpp {__version__}")
    parser.add_argument("--root", help="ledger root (default ~/.claude/skillpp)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("hook", help="hook entry point (reads JSON on stdin)")
    p.add_argument("--event", help="override hook_event_name")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("prepare-session",
                       help="print a session's new work for review")
    p.add_argument("target", nargs="?",
                   help="transcript path or session id (default: this session)")
    p.add_argument("--window", type=int, metavar="N",
                   help="print only window N instead of the whole session")
    p.add_argument("--windows", action="store_true",
                   help="list the windows and their sizes, printing none")
    p.set_defaults(func=cmd_prepare_session)

    p = sub.add_parser("segments",
                       help="print a session's requests and their work, numbered")
    p.add_argument("target", nargs="?",
                   help="transcript path or session id (default: this session)")
    p.add_argument("--aspect", action="append",
                   help="show only these parts of each request: tools, "
                        "prompts, narration, results, failures. Repeatable. "
                        "Default shows all.")
    p.add_argument("--only", type=int, metavar="N",
                   help="print just segment N, for asking one question about "
                        "one request")
    p.set_defaults(func=cmd_segments)

    p = sub.add_parser("locate",
                       help="run the local cheap pass: which requests landed")
    p.add_argument("target", nargs="?",
                   help="transcript path or session id (default: this session)")
    p.add_argument("--pair", action="store_true",
                   help="run a strict and a lenient model and escalate where "
                        "they disagree")
    p.add_argument("--model", action="append", help="override the models used")
    p.add_argument("--aspect", action="append",
                   help="show the models only these parts of each request")
    p.set_defaults(func=cmd_locate)

    p = sub.add_parser("drain",
                       help="review sessions that ended without being reviewed")
    p.add_argument("--apply", action="store_true",
                   help="actually run the reviews; each is a model call")
    p.add_argument("--limit", type=int, help="review at most this many")
    p.add_argument("--queue", help="path to ended-sessions.jsonl")
    p.add_argument("--prune", action="store_true",
                   help="drop entries whose transcript no longer exists; a "
                        "`claude -p` run leaves one every time")
    p.set_defaults(func=cmd_drain)

    p = sub.add_parser("candidates", help="list what has been proposed so far")
    p.add_argument("--json", action="store_true")
    p.add_argument("--open-only", action="store_true",
                   help="only those not yet made into skills")
    p.set_defaults(func=cmd_candidates)

    p = sub.add_parser("promote-candidate",
                       help="write a candidate out as a skill and record it")
    p.add_argument("name")
    p.add_argument("--description", help="what it does and when to use it")
    p.add_argument("--when-to-use",
                   help="trigger phrases or example requests, appended to the "
                        "description in the skill listing")
    p.add_argument("--skills-dir")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing SKILL.md")
    p.set_defaults(func=cmd_promote_candidate)

    p = sub.add_parser("record-candidate",
                       help="fold one proposed skill into the memory (body on stdin)")
    p.add_argument("target", nargs="?",
                   help="transcript path or session id (default: this session)")
    p.add_argument("--name", required=True, help="the skill's name")
    p.add_argument("--matches",
                   help="an existing candidate this is the same procedure as")
    p.set_defaults(func=cmd_record_candidate)

    p = sub.add_parser("commit-session",
                       help="declare the session reviewed (advances the watermark)")
    p.add_argument("target", nargs="?",
                   help="transcript path or session id (default: this session)")
    p.set_defaults(func=cmd_commit_session)

    p = sub.add_parser("review", help="list candidates ready for review")
    p.add_argument("--all", action="store_true", help="include below-threshold candidates")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("dictate", help="describe a workflow instead of performing it")
    p.add_argument("--text", help="the description (reads stdin if omitted)")
    p.add_argument("--title")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dictate)

    p = sub.add_parser("show", help="effect summary, evidence and open questions")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("search", help="search the ledger of your own past work")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("stats", help="ledger size and status counts")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("scaffold", help="generate a starting SKILL.md for a candidate")
    p.add_argument("id")
    p.add_argument("--name", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--answers", help="JSON object of answered questions")
    p.add_argument("--tier", default="provisional", choices=["provisional", "trusted"])
    p.add_argument("--out", help="write to this path instead of stdout")
    p.set_defaults(func=cmd_scaffold)

    p = sub.add_parser("promote", help="mark a candidate promoted")
    p.add_argument("id")
    p.add_argument("--skill-path")
    p.add_argument("--note")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("ignore", help="never propose this workflow again")
    p.add_argument("id")
    p.add_argument("--note")
    p.set_defaults(func=cmd_ignore)

    p = sub.add_parser("ignored", help="list ignored workflows")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ignored)

    p = sub.add_parser("reopen", help="return a workflow to the review queue")
    p.add_argument("id")
    p.add_argument("--note")
    p.set_defaults(func=cmd_reopen)

    p = sub.add_parser("expire", help="delete unapproved candidates past their TTL")
    p.set_defaults(func=cmd_expire)

    p = sub.add_parser("lifecycle", help="inventory skills, tiers and staleness")
    p.add_argument("--skills-dir")
    p.add_argument("--project-root")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_lifecycle)

    p = sub.add_parser("reconcile",
                       help="report drift between the ledger and skills on disk")
    p.add_argument("--apply", action="store_true",
                   help="park deleted skills' workflows in the ignore list "
                        "(visible and reversible; never re-proposes them)")
    p.add_argument("--skills-dir")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("tier", help="move a skill between hot/cold/archived")
    p.add_argument("name")
    p.add_argument("tier", choices=["hot", "cold", "archived"])
    p.add_argument("--skills-dir")
    p.set_defaults(func=cmd_tier)

    p = sub.add_parser("check", help="dependency check at pull time")
    p.add_argument("--name", help="skill name")
    p.add_argument("--id", help="ledger candidate id")
    p.add_argument("--skills-dir")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("bundle", help="package skills for distribution")
    p.add_argument("--out", required=True, help="output directory for the bundle")
    p.add_argument("--format", choices=["plugin", "upload"], default="plugin",
                   help="plugin: .claude-plugin/ + skills/ (team, Claude Code). "
                        "upload: one <name>.zip per skill (Customize → Skills)")
    p.add_argument("--plugin-name", default="my-skills")
    p.add_argument("--plugin-version", default="1.0.0")
    p.add_argument("--description", default="Skills captured with Skill Plus Plus.")
    p.add_argument("--name-filter", nargs="*", help="only these skill names")
    p.add_argument("--skills-dir")
    p.add_argument("--include-cold", action="store_true")
    p.add_argument("--with-commands", action="store_true",
                   help="include the skillpp slash commands")
    p.add_argument("--zip", action="store_true", help="also produce a .zip")
    p.set_defaults(func=cmd_bundle)

    p = sub.add_parser("install", help="wire Claude Code hooks (dry run by default)")
    p.add_argument("--apply", action="store_true", help="actually write settings.json")
    p.add_argument("--settings")
    p.add_argument("--commands-dir")
    p.set_defaults(func=cmd_install)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check" and not args.name and not args.id:
        print("check requires --name or --id", file=sys.stderr)
        return 1
    return args.func(args)
