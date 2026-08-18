"""Everything that must happen before a model looks at a session.

Resolving the transcript, working out which conversation it belongs to, finding
what is new since last time, and compressing that down. All of it has exactly
one right answer, and all of it fails *silently* when a model improvises it
instead -- a slightly different slice re-counts work already recorded, a
slightly different id opens a second memory file for the same conversation.

So it lives here, and the skill that calls this is left with the one job that
genuinely needs judgement: deciding what in the session is worth keeping.

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

from .config import Config
from .lifecycle import parse_frontmatter
from .extract import extract
from .identity import conversation_id
from .transcript import Message, read

PROJECTS = Path.home() / ".claude" / "projects"

# Below this many new messages a run is not worth its fixed cost: loading the
# context dominates, and the messages are not lost -- the watermark does not
# advance, so they arrive in the next run instead.
MIN_NEW_MESSAGES = 25


class TranscriptNotFound(FileNotFoundError):
    """Nothing resolvable at the path or session id given."""


@dataclass
class Prepared:
    """What a session offers up for review, and where it came from."""

    transcript: Path
    conversation: str
    config: Config
    memory_path: Path
    memory: str  # the document as it stands, empty on a first run
    new_messages: list[Message]
    material: str  # the extracted new work
    watermark: tuple[str, str] | None  # (uuid, timestamp) to commit afterwards
    total_messages: int

    @property
    def is_first_run(self) -> bool:
        return not self.memory

    @property
    def has_enough(self) -> bool:
        return len(self.new_messages) >= MIN_NEW_MESSAGES


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


def _watermark(memory: str) -> tuple[str, str] | None:
    front = parse_frontmatter(memory)
    uuid = str(front.get("last_message") or "")
    stamp = str(front.get("last_timestamp") or "")
    return (uuid, stamp) if uuid or stamp else None


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
    memory_path = config.conversations_dir / f"{conversation}.md"
    memory = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""

    new = _slice(messages, _watermark(memory))
    mark = (new[-1].uuid, new[-1].timestamp) if new else None

    return Prepared(
        transcript=path,
        conversation=conversation,
        config=config,
        memory_path=memory_path,
        memory=memory,
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
        f"memory file    {prepared.memory_path}",
        f"new messages   {len(prepared.new_messages)} of {prepared.total_messages}",
    ]

    if not prepared.new_messages:
        return "\n".join(head + [
            "", "Nothing has happened in this conversation since it was last "
            "reviewed. Stop here and write nothing."])

    if not prepared.has_enough:
        return "\n".join(head + [
            "", f"Only {len(prepared.new_messages)} new messages, below the "
            f"{MIN_NEW_MESSAGES} needed to be worth a review. Stop here and "
            f"write nothing — they stay unread and will arrive with the next "
            f"batch."])

    parts = ["\n".join(head), ""]
    if prepared.is_first_run:
        parts += ["# The conversation's memory so far",
                  "", "This conversation has not been reviewed before.", ""]
    else:
        parts += ["# The conversation's memory so far",
                  "", prepared.memory.strip(), ""]
    parts += ["# New since the last review", "", prepared.material]
    return "\n".join(parts)


def _write(prepared: Prepared, body: str, mark: tuple[str, str] | None,
           now: datetime | None = None) -> Path:
    """Rewrite the memory document atomically."""
    front = ["---", f"conversation: {prepared.conversation}"]
    if mark:
        front += [f"last_message: {mark[0]}", f"last_timestamp: {mark[1]}"]
    front += [f"reviewed: {(now or datetime.now(timezone.utc)).date().isoformat()}",
              "---", ""]

    prepared.memory_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = prepared.memory_path.with_suffix(".md.tmp")
    temporary.write_text("\n".join(front) + body.strip() + "\n", encoding="utf-8")
    temporary.replace(prepared.memory_path)
    return prepared.memory_path


def _existing_mark(memory: str) -> tuple[str, str] | None:
    return _watermark(memory)


def _append_review(prepared: Prepared, config: Config, *, name: str, body: str,
                   matches: str | None, now: datetime | None = None) -> Path:
    """Record what this review proposed, in the words it proposed it.

    The conversation memory keeps only the current best description of each
    procedure, because that is what it is for. This keeps the deliveries: what
    each session actually said, before it was merged into anything.

    Worth the duplication for two reasons. The prompt producing these is still
    changing, and a revised one can be re-merged from proposals already judged
    rather than by re-reading transcripts. And when an entry in the memory
    looks wrong, this says which session introduced it and what it claimed at
    the time.
    """
    session = prepared.transcript.stem
    path = config.reviews_dir / f"{session}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        stamped = (now or datetime.now(timezone.utc)).date().isoformat()
        path.write_text("\n".join([
            "---",
            f"session: {session}",
            f"conversation: {prepared.conversation}",
            f"reviewed: {stamped}",
            "---",
            "",
        ]), encoding="utf-8")

    verdict = f"merged into: {matches}" if matches else "new"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {name}\n{verdict}\n\n{body.strip()}\n")
    return path


def record(prepared: Prepared, *, name: str, body: str,
           matches: str | None = None, now: datetime | None = None) -> Path:
    """Fold one proposed skill into the memory. Leaves the watermark alone.

    Recording a candidate and declaring the session reviewed are separate acts:
    a session may yield three candidates or none, and the watermark should move
    exactly once either way.
    """
    from .memory import parse, render as render_memory, upsert

    # The full stem, never a prefix of it. An earlier version truncated to
    # eight characters to keep the provenance line short, which silently
    # merged any two transcripts sharing an opening -- the second was read as
    # already present, so the count stopped rising and the evidence for a
    # candidate quietly stalled. Brevity is not worth a wrong count.
    session = prepared.transcript.stem
    updated = upsert(parse(prepared.memory), name=name, body=body,
                     session=session, matches=matches)
    written = _write(prepared, render_memory(updated),
                     _existing_mark(prepared.memory), now)
    prepared.memory = written.read_text(encoding="utf-8")
    _append_review(prepared, prepared.config, name=name, body=body,
                   matches=matches, now=now)
    return written


def commit(prepared: Prepared, *, now: datetime | None = None) -> Path:
    """Declare the session reviewed by advancing the watermark.

    Called after any candidates are recorded, never before: a watermark moved
    ahead of a failed review buries those messages permanently, behind a mark
    saying they have already been read.
    """
    if not prepared.watermark:
        raise ValueError("nothing was prepared, so there is nothing to commit")
    body = prepared.memory
    match = re.search(r"\n---\n", body)
    if body.startswith("---") and match:
        body = body[match.end():]
    return _write(prepared, body or "## Skill candidates\n\n_None yet._\n",
                  prepared.watermark, now)
