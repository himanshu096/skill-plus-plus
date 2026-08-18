"""Filesystem layout and tunable thresholds.

Everything is overridable by environment variable so the test-suite (and a
curious user) can point the whole engine at a scratch directory without
touching a real ledger.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".claude" / "skillpp"


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

    @property
    def ledger_dir(self) -> Path:
        return self.root / "ledger"

    @property
    def sessions_dir(self) -> Path:
        """Per-session capture state, written by the hooks as JSON.

        Not to be confused with :attr:`conversations_dir`, which holds the
        reviewed memory documents. This one is a working buffer keyed by
        session id; that one is the durable record keyed by conversation.
        """
        return self.root / "sessions"

    @property
    def conversations_dir(self) -> Path:
        """One memory document per conversation, surviving every resume."""
        return self.root / "conversations"

    @property
    def reviews_dir(self) -> Path:
        """What each individual review proposed, as it proposed it.

        The conversation memory is the store; these are the deliveries into it.
        Kept because the prompt that produces them keeps changing: with these,
        a revised prompt can be re-merged from what was already judged, and a
        surprising entry can be traced to the session that introduced it.
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
    """Project-local skills directory, falling back to the personal one."""
    base = Path(cwd) if cwd else Path.cwd()
    project = base / ".claude" / "skills"
    if project.exists():
        return project
    return Path.home() / ".claude" / "skills"
