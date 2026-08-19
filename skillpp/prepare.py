"""Everything that must happen before a model looks at a session.

Resolving the transcript, working out which conversation it belongs to, finding
what is new since last time, and compressing that down. All of it has exactly
one right answer, and all of it fails *silently* when a model improvises it
instead -- a slightly different slice re-counts work already recorded, a
slightly different id reads a conversation from its beginning again.

So it lives here, and the skill that calls this is left with the one job that
genuinely needs judgement: deciding what in the session is worth keeping.

The store is a directory of entry files plus two append-only logs, described
in ``memory.py``. Each review also leaves ``reviews/<session-id>.md``, holding
what that session proposed *and* how far it read.

There is deliberately no per-conversation document. An earlier design had one,
which meant a candidate's count could only rise when a single conversation
repeated a whole procedure -- and 82% of conversations here are a single
session, so counts sat at 1 and the ordering they were meant to drive did
nothing. Pooling every session into one store is what makes recurrence visible.

A single *document* was the next attempt and was worse: re-serialising every
entry on every write meant one bad read wrote a truncated body back over a
good one, and the damage was not confined to the entry being touched.

The conversation id survives that simplification, but only as a bookmark. A
resumed session's transcript contains the entire earlier history again --
measured here, 117 transcript files for 77 conversations -- so a watermark keyed
on the session would read those messages as new every time and record the same
work twice.

The output is written to be read by a model, not parsed: it goes straight into
a prompt via a `` !`command` `` substitution, so "there is nothing to do" has to
be a sentence rather than an exit code.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import MIN_NEW_MESSAGES as _MIN_NEW_MESSAGES, Config
from .extract import extract
from .identity import conversation_id
from .lifecycle import parse_frontmatter
from .transcript import Message, read

PROJECTS = Path.home() / ".claude" / "projects"

# Re-exported from config, where every other threshold lives and where this
# one is read from the environment. Kept importable under this name because it
# is what the tests assert the default against.
MIN_NEW_MESSAGES = _MIN_NEW_MESSAGES


class TranscriptNotFound(FileNotFoundError):
    """Nothing resolvable at the path or session id given."""


@dataclass
class Prepared:
    """What a session offers up for review, and where it came from."""

    transcript: Path
    conversation: str
    config: Config
    patterns: str  # the store as it stands, empty before anything is recorded
    new_messages: list[Message]
    material: str  # the extracted new work
    watermark: tuple[str, str] | None  # (uuid, timestamp) to record afterwards
    total_messages: int

    @property
    def session(self) -> str:
        return self.transcript.stem

    @property
    def review_path(self) -> Path:
        return self.config.reviews_dir / f"{self.session}.md"

    @property
    def has_enough(self) -> bool:
        return len(self.new_messages) >= self.config.min_new_messages


def project_slug(cwd: str | Path | None = None) -> str:
    """The directory name Claude Code files a project's transcripts under.

    Every ``/``, ``.`` and ``_`` becomes ``-``. Replacing only the slashes
    yields a path that does not exist, which is a slow thing to debug.
    """
    return re.sub(r"[/._]", "-", str(Path(cwd or Path.cwd()).resolve()))


def resolve(target: str | None = None, cwd: str | Path | None = None) -> Path:
    """Find a transcript from a path, a session id, or the environment."""
    if target:
        candidate = Path(target).expanduser()
        if candidate.is_file():
            return candidate
        # A path that was meant as a path is not a session id to search for:
        # globbing one produces a confusing crash rather than a useful error.
        if candidate.is_absolute() or "/" in target:
            raise TranscriptNotFound(f"no transcript at {candidate}")
    session = target or os.environ.get("CLAUDE_SESSION_ID") \
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session:
        raise TranscriptNotFound(
            "no transcript given and no session id in the environment")

    directory = PROJECTS / project_slug(cwd)
    exact = directory / f"{session}.jsonl"
    if exact.is_file():
        return exact
    # A short id is far easier to type, and unambiguous in practice.
    matches = sorted(directory.glob(f"{session}*.jsonl"))
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise TranscriptNotFound(
            f"'{session}' matches {len(matches)} transcripts in {directory}")
    raise TranscriptNotFound(f"no transcript for '{session}' in {directory}")


# --------------------------------------------------------------------------
# the bookmark: how far a conversation has been read
# --------------------------------------------------------------------------

def reviews_for(config: Config, conversation: str) -> list[dict]:
    """Frontmatter of every review belonging to one conversation."""
    if not config.reviews_dir.exists():
        return []
    out = []
    for path in sorted(config.reviews_dir.glob("*.md")):
        try:
            front = parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if str(front.get("conversation") or "") == conversation:
            out.append(front)
    return out


def watermark_for(config: Config, conversation: str) -> tuple[str, str] | None:
    """The furthest point this conversation has been read to.

    Taken from the reviews rather than a store of its own: a review already
    records which conversation it belongs to, so two more fields make it the
    bookmark and remove a third place for state to live and disagree.
    """
    marks = [(str(f.get("last_message") or ""), str(f.get("last_timestamp") or ""))
             for f in reviews_for(config, conversation)]
    marks = [m for m in marks if m[0] or m[1]]
    if not marks:
        return None
    # Furthest, not most recently written: reviews can be run out of order.
    return max(marks, key=lambda m: m[1])


def _slice(messages: list[Message], mark: tuple[str, str] | None) -> list[Message]:
    """The messages after the watermark.

    Two ways to find the boundary, because the first one is not reliable. A
    resumed session can regenerate the tail of the conversation -- interrupt a
    tool call and the turn is rewritten -- so the message the mark names may no
    longer exist. Timestamps survive that.

    If neither resolves, this raises rather than guessing. The tempting guess
    ("treat it all as new") silently re-folds an entire conversation and
    doubles every count in it.

    Known limitation: comparing timestamps splits a turn that was regenerated
    partly before and partly after the marked moment -- the earlier half is
    treated as already seen. Nothing reviewed is ever repeated, which is the
    guarantee worth keeping, but a sliver of a rewritten turn can be missed.
    """
    if not mark:
        return messages
    uuid, stamp = mark

    for index, message in enumerate(messages):
        if message.uuid == uuid:
            return messages[index + 1:]

    if stamp:
        after = [m for m in messages if m.timestamp and m.timestamp > stamp]
        if after or any(m.timestamp for m in messages):
            return after

    raise ValueError(
        f"watermark {uuid or stamp!r} is not in this transcript and no "
        f"timestamp resolves it. Refusing to guess: treating the conversation "
        f"as new would re-record work already captured."
    )


def prepare(target: str | None = None, config: Config | None = None,
            cwd: str | Path | None = None) -> Prepared:
    """Gather everything a session review needs, and nothing it does not."""
    config = config or Config()
    path = resolve(target, cwd)
    messages = read(path)
    if not messages:
        raise TranscriptNotFound(f"{path} holds no conversation messages")

    conversation = conversation_id(messages)
    # Rendered on demand from the entry files. There is no store document on
    # disk to fall out of step with them.
    from .memory import load, render as render_store
    entries = load(config)
    patterns = render_store(entries) if entries else ""

    new = _slice(messages, watermark_for(config, conversation))
    mark = (new[-1].uuid, new[-1].timestamp) if new else None

    return Prepared(
        transcript=path,
        conversation=conversation,
        config=config,
        patterns=patterns,
        new_messages=new,
        material=extract(new) if new else "",
        watermark=mark,
        total_messages=len(messages),
    )


def render(prepared: Prepared) -> str:
    """The prepared session as text for a prompt.

    Where there is nothing to do, that is stated as an instruction rather than
    signalled by an exit code: the caller substitutes this straight into a
    prompt and never sees a status.
    """
    head = [
        f"conversation   {prepared.conversation}",
        f"transcript     {prepared.transcript.name}",
        # The root, not the entry directory. A model handed a path uses it:
        # printing `<root>/patterns` here got it passed back as `--root`, and
        # the whole store was rebuilt one level deeper with nothing reporting
        # a problem. This path is safe to echo back because it is the one the
        # flag actually takes.
        f"store          {prepared.config.root}",
        f"new messages   {len(prepared.new_messages)} of {prepared.total_messages}",
    ]

    if not prepared.new_messages:
        return "\n".join(head + [
            "", "Nothing has happened in this conversation since it was last "
            "reviewed. Stop here and write nothing."])

    if not prepared.has_enough:
        return "\n".join(head + [
            "", f"Only {len(prepared.new_messages)} new messages, below the "
            f"{prepared.config.min_new_messages} needed to be worth a "
            f"review. Stop here and "
            f"write nothing — they stay unread and will arrive with the next "
            f"batch."])

    parts = ["\n".join(head), "", "# Candidates recorded so far", ""]
    parts += [prepared.patterns.strip() or "Nothing has been recorded yet.", ""]
    parts += ["# New in this session", "", prepared.material]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def _write_atomic(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(body, encoding="utf-8")
    temporary.replace(path)
    return path


def _write_review(prepared: Prepared, proposals: str, *,
                  mark: tuple[str, str] | None,
                  now: datetime | None = None) -> Path:
    """Record what this session proposed, and how far it read.

    Written even when a session proposes nothing. A barren session still has to
    leave a bookmark or its messages are read again forever -- and "reviewed,
    found nothing" is worth knowing on its own, being the only evidence of
    whether the filter is too strict.
    """
    front = [
        "---",
        f"session: {prepared.session}",
        f"conversation: {prepared.conversation}",
    ]
    if mark:
        front += [f"last_message: {mark[0]}", f"last_timestamp: {mark[1]}"]
    front += [f"reviewed: {(now or datetime.now(timezone.utc)).date().isoformat()}",
              "---", ""]
    body = proposals.strip() or "_No candidate found in this session._"
    return _write_atomic(prepared.review_path, "\n".join(front) + body + "\n")


def _proposals_in(review: Path) -> str:
    """The body of an existing review, so a second candidate does not lose it."""
    if not review.exists():
        return ""
    text = review.read_text(encoding="utf-8")
    match = re.search(r"\n---\n", text)
    body = text[match.end():] if text.startswith("---") and match else text
    return "" if body.strip().startswith("_No candidate") else body.strip()


def _existing_mark(prepared: Prepared) -> tuple[str, str] | None:
    front = parse_frontmatter(prepared.review_path.read_text(encoding="utf-8")) \
        if prepared.review_path.exists() else {}
    uuid = str(front.get("last_message") or "")
    stamp = str(front.get("last_timestamp") or "")
    return (uuid, stamp) if uuid or stamp else None


def record(prepared: Prepared, *, name: str, body: str,
           matches: str | None = None, now: datetime | None = None) -> Path:
    """Fold one proposed skill into the store. Leaves the bookmark alone.

    Recording a candidate and declaring the session reviewed are separate acts:
    a session may yield three candidates or none, and the bookmark should move
    exactly once either way.
    """
    from .memory import (add_entry, add_occurrence, entry_path, find, load,
                         render as render_store)

    config = prepared.config
    if matches:
        # A match increments the evidence and nothing else. The stored body is
        # what the first occurrence taught; a later account of the same
        # procedure lives on in its own review file rather than overwriting it.
        target = find(load(config), matches)
        if target is None:
            raise KeyError(
                f"no candidate named {matches!r} to match against; "
                f"have: {', '.join(c.name for c in load(config)) or '(none)'}")
        add_occurrence(config, name=target.name, session=prepared.session)
        written = target.path or entry_path(config, target.name)
        # Not "merged": the stored body is untouched. Saying otherwise sent a
        # model off to combine two bodies whose result was then dropped.
        verdict = f"another sighting of: {target.name}"
    else:
        written = add_entry(config, name=name, body=body)
        add_occurrence(config, name=name, session=prepared.session)
        verdict = "new"

    prepared.patterns = render_store(load(config))
    existing = _proposals_in(prepared.review_path)
    proposals = f"{existing}\n\n## {name}\n{verdict}\n\n{body.strip()}".strip()
    # Carries forward whatever mark is already there rather than writing one:
    # recording is not reviewing, and a mark set now would survive a later
    # failure as a claim that these messages had been read.
    _write_review(prepared, proposals, mark=_existing_mark(prepared), now=now)
    return written


def commit(prepared: Prepared, *, now: datetime | None = None) -> Path:
    """Declare the session reviewed by writing the bookmark.

    Called after any candidates are recorded, never before: a bookmark moved
    ahead of a failed review buries those messages permanently, behind a mark
    saying they have already been read.
    """
    if not prepared.watermark:
        raise ValueError("nothing was prepared, so there is nothing to commit")
    return _write_review(prepared, _proposals_in(prepared.review_path),
                         mark=prepared.watermark, now=now)
