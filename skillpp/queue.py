"""The ended-sessions queue: what `drain` reads, and the only thing a hook writes.

One module for one reason. Before this existed the queue location was written
down twice — once in ``cli.py``'s drain default, once in a hand-typed
``python3 -c`` one-liner in ``.claude/settings.json`` — and nothing made the two
agree. A queue whose writer and reader disagree about its path is empty in a way
that looks exactly like "no sessions ended yet".

The queue is **project-local** (``<project>/.claude/skillpp/memory/``) while the
store is global (``~/.claude/skillpp/``). That is deliberate: a queued session
names a transcript belonging to one repo, and draining is something you do in
that repo.
"""

from __future__ import annotations

import json
from pathlib import Path

QUEUE_NAME = "ended-sessions.jsonl"


def queue_path(base: Path | str | None = None) -> Path:
    """Where the queue lives for a given project directory.

    ``base`` is the project root — the hook passes the payload's ``cwd`` (the
    hook process may be started anywhere), ``drain`` passes ``Path.cwd()``.
    """
    root = Path(base).expanduser() if base else Path.cwd()
    return root / ".claude" / "skillpp" / "memory" / QUEUE_NAME


def enqueue(payload: dict) -> dict:
    """Append one ended session to its project's queue.

    Skips a payload with no readable transcript. ``drain`` already counts a
    transcript that has since disappeared, but there is no reason to bank a row
    that was never usable — and a session with no transcript at all is not one
    that can be reviewed later.
    """
    transcript = str(payload.get("transcript_path") or "")
    if not transcript or not Path(transcript).expanduser().is_file():
        return {"status": "no-transcript"}

    path = queue_path(payload.get("cwd"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")
    return {"status": "queued",
            "session": str(payload.get("session_id") or ""),
            "queue": str(path)}
