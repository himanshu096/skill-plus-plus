"""Filesystem layout and tunable thresholds.

Everything is overridable by environment variable so the test-suite (and a
curious user) can point the whole engine at a scratch directory without
touching a real ledger.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".claude" / "skillpp"

# The messages are never lost when a run is skipped for being too small:
# the bookmark does not advance, so they arrive with the next batch.
MIN_NEW_MESSAGES = 25


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


class Config:
    """Resolved paths and thresholds for one invocation."""

    def __init__(self, root: str | Path | None = None) -> None:
        if root is not None:
            self.root = Path(root).expanduser()
        elif os.environ.get("SKILLPP_ROOT"):
            self.root = Path(os.environ["SKILLPP_ROOT"]).expanduser()
        else:
            self.root = DEFAULT_ROOT

        # A workflow must recur this many times before it is proposed (README 3.3).
        self.recurrence_threshold = _int_env("SKILLPP_RECURRENCE", 3)
        # Lexical similarity above which two traces are considered the same workflow.
        self.similarity_threshold = _float_env("SKILLPP_SIMILARITY", 0.85)
        # Unapproved candidates self-delete after this long (README 5).
        self.candidate_ttl_days = _int_env("SKILLPP_TTL_DAYS", 14)
        # Hard caps so a runaway session cannot bloat the ledger.
        self.max_steps_per_session = _int_env("SKILLPP_MAX_STEPS", 500)
        self.max_field_chars = _int_env("SKILLPP_MAX_FIELD", 2000)
        # Never ask the developer more than this many questions (README 4).
        self.max_questions = _int_env("SKILLPP_MAX_QUESTIONS", 3)
        # Below this many new messages a review is not worth its fixed cost.
        # Overridable because it is a cost guard rather than a judgement: an
        # eval deliberately pays that cost on a small session, and until this
        # was here it could not, so a short fixture returned "stop" and the
        # detection question was never put to the model at all.
        self.min_new_messages = _int_env("SKILLPP_MIN_NEW", MIN_NEW_MESSAGES)

    @property
    def ledger_dir(self) -> Path:
        return self.root / "ledger"

    @property
    def sessions_dir(self) -> Path:
        """Per-session capture state, written by the hooks as JSON.

        Not to be confused with :attr:`reviews_dir`. This is a working buffer
        the hooks write while a session runs; that is what a review concluded
        about a session afterwards.
        """
        return self.root / "sessions"

    @property
    def patterns_dir(self) -> Path:
        """One file per candidate, each written once and never rewritten.

        A single document could not hold what it existed to hold. Its parser
        ended an entry at the next ``##`` heading, and skill bodies use ``##``
        for their own rules, so every body was cut at its first rule -- and
        because a write re-serialised the whole document, recording against
        one entry destroyed the bodies of entries nobody had touched.

        One file per entry removes the failure rather than patching it: there
        are no boundaries to find, and nothing re-serialises a body.
        """
        return self.root / "patterns"

    @property
    def occurrences_file(self) -> Path:
        """Append-only log of sightings; the only thing a match writes.

        The count was already derived from provenance rather than stored, so
        "seen once more" is one appended line. Nothing is read to write it,
        which is what makes a bad read unable to corrupt anything.
        """
        return self.root / "occurrences.jsonl"

    @property
    def decisions_file(self) -> Path:
        """Append-only log of what a person decided, and when.

        Status is the last decision for a name rather than a field that gets
        overwritten, so promotion history survives instead of being replaced.
        """
        return self.root / "decisions.jsonl"

    @property
    def exemplars_file(self) -> Path:
        """Append-only log of what a procedure's sessions look like, embedded.

        One line per sighting, because matching a session against a written
        body does not work: a session sits near-equidistant from every body in
        the store (a spread of 0.07 across all of them), while two sessions
        doing the same procedure are separable. Comparing like with like needs
        something session-shaped to compare against, and this is it.
        """
        return self.root / "exemplars.jsonl"

    @property
    def reviews_dir(self) -> Path:
        """What each individual review proposed, as it proposed it.

        :attr:`patterns_dir` is the store; these are the deliveries into it,
        and they double as the bookmark -- each records how far its session
        read, which is what stops a resumed conversation being re-read.

        Kept in their own right because the prompt that produces them keeps
        changing: a revised prompt can be re-merged from proposals already
        judged, and a surprising entry can be traced back to the session that
        introduced it and what it claimed at the time.
        """
        return self.root / "reviews"

    @property
    def cold_dir(self) -> Path:
        return self.root / "cold"

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    @property
    def log_file(self) -> Path:
        return self.root / "skillpp.log"

    def ensure_dirs(self) -> None:
        for d in (self.ledger_dir, self.sessions_dir, self.cold_dir, self.archive_dir):
            d.mkdir(parents=True, exist_ok=True)


def default_skills_dir(cwd: str | Path | None = None) -> Path:
    """Project-local skills directory, falling back to the personal one.

    ``SKILLPP_SKILLS_DIR`` overrides both. Without it this module's promise
    that everything is redirectable by environment stopped one step short of
    the only command that writes outside the root: pointing ``SKILLPP_ROOT`` at
    a scratch directory still left a promotion landing in the real
    ``~/.claude/skills``.
    """
    if os.environ.get("SKILLPP_SKILLS_DIR"):
        return Path(os.environ["SKILLPP_SKILLS_DIR"]).expanduser()
    base = Path(cwd) if cwd else Path.cwd()
    project = base / ".claude" / "skills"
    if project.exists():
        return project
    return Path.home() / ".claude" / "skills"
