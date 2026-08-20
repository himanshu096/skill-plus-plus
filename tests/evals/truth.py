"""Ground truth for triage, read out of the reviews the pipeline already wrote.

Every session the pipeline has reviewed left a file in `reviews/` saying what it
proposed. That is a labelled set of *real* sessions, produced by the frontier
judge rather than by hand — which is what the fixtures in `locator.py` and
`cold.py` are not, and the reason three separate defects in this work were
invisible until real data.

The label is coarse on purpose: **did this session contain anything worth
recording?** That is the question a cheap session-level pass has to answer, and
it is a different and easier question than where inside the session the work
landed — the one the span pass answered correctly and uselessly.

Parsing note, because getting this wrong silently invalidates any score built on
it: a proposal is a ``##`` heading whose next line marks an outcome. Three
markers exist across the reviews written so far -- ``new``, ``another sighting
of:`` and an older ``merged into:`` -- and a heading followed by a blank line is
a section *inside* a proposal's body, not a proposal. Missing ``merged into:``
labelled a session "nothing" that had in fact matched an existing candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REVIEWS = Path.home() / ".claude" / "skillpp" / "reviews"
PROJECTS = Path.home() / ".claude" / "projects"
MARKERS = ("new", "another sighting of:", "merged into:")


@dataclass
class Session:
    """One reviewed session, its transcript, and what the judge concluded."""

    session: str
    transcript: Path
    proposals: list[str]

    @property
    def has_procedure(self) -> bool:
        return bool(self.proposals)


def proposals_in(review: Path) -> list[str]:
    lines = review.read_text(encoding="utf-8", errors="replace").splitlines()
    out = []
    for i, line in enumerate(lines[:-1]):
        if not line.startswith("## "):
            continue
        after = lines[i + 1].strip()
        if any(after.startswith(m) for m in MARKERS):
            out.append(line[3:].strip())
    return out


def labelled(reviews: Path = REVIEWS) -> list[Session]:
    """Reviewed sessions whose transcript is still on disk.

    A transcript that is gone cannot be re-read, so it cannot be scored however
    good the label is.
    """
    live = {p.stem: p for p in PROJECTS.glob("*/*.jsonl")}
    out = []
    for review in sorted(reviews.glob("*.md")):
        path = live.get(review.stem)
        if path is None:
            continue
        out.append(Session(session=review.stem, transcript=path,
                           proposals=proposals_in(review)))
    return out
