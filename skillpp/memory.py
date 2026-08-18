"""The conversation's memory document: parsing, updating, rendering.

Everything here is bookkeeping, and it exists so that a model never has to do
it. An earlier design had the model rewrite the whole document on each review —
carrying every existing candidate forward by hand, incrementing counts, keeping
the order. That works until it doesn't, and when it doesn't the failure is a
candidate quietly missing from a file that may hold months of them.

So the model is left with the one question it is actually needed for: *does
this proposal describe a procedure already in here, or a new one?* It answers
with a name and a body. Counting, provenance, ordering and rewriting happen
here, where they cannot be forgotten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADING = re.compile(r"^###\s+(.+?)\s*$", re.M)
SEEN = re.compile(r"^\*\*seen\s+(\d+)×\*\*\s*(?:·\s*(.*))?$", re.M)
ALIAS = re.compile(r"^also seen as:\s*(.*)$", re.M)
SECTION = "## Skill candidates"


@dataclass
class Candidate:
    """One proposed skill, and the evidence for it."""

    name: str
    body: str = ""
    sessions: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        """How many times this has been seen. Ordering only, never a gate."""
        return len(self.sessions)

    def render(self) -> str:
        lines = [f"### {self.name}", f"**seen {self.count}×** · " + ", ".join(self.sessions)]
        if self.aliases:
            lines.append("also seen as: " + ", ".join(f'"{a}"' for a in self.aliases))
        return "\n".join(lines) + "\n\n" + self.body.strip() + "\n"


def parse(document: str) -> list[Candidate]:
    """Read the candidates out of a memory document.

    Tolerant by design: a hand-edited document should still load. A heading
    with no `seen` line is a candidate seen zero times, not a parse error.
    """
    out: list[Candidate] = []
    matches = list(HEADING.finditer(document))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(document)
        block = document[match.end():end]

        sessions: list[str] = []
        seen = SEEN.search(block)
        if seen and seen.group(2):
            sessions = [s.strip() for s in seen.group(2).split(",") if s.strip()]

        aliases: list[str] = []
        alias = ALIAS.search(block)
        if alias:
            aliases = [a.strip().strip('"') for a in alias.group(1).split(",") if a.strip()]

        body = block
        for pattern in (SEEN, ALIAS):
            found = pattern.search(body)
            if found:
                body = body[:found.start()] + body[found.end():]
        out.append(Candidate(match.group(1).strip(), body.strip(), sessions, aliases))
    return out


def upsert(candidates: list[Candidate], *, name: str, body: str, session: str,
           matches: str | None = None) -> list[Candidate]:
    """Fold one proposal in, and return the list in reading order.

    ``matches`` names an existing candidate the model judged to be the same
    procedure. Everything that follows from that — the count, the provenance,
    recording the old name when it changes — is decided here rather than
    written out by the model.
    """
    out = list(candidates)
    existing = None
    if matches:
        wanted = matches.strip().lower()
        existing = next((c for c in out if c.name.lower() == wanted
                         or wanted in (a.lower() for a in c.aliases)), None)
        if existing is None:
            raise KeyError(
                f"no candidate named {matches!r} to match against; "
                f"have: {', '.join(c.name for c in out) or '(none)'}")

    if existing is None:
        out.append(Candidate(name, body, [session]))
    else:
        # A later description of the same procedure is usually the better one:
        # it was written knowing more. The earlier name is kept as an alias so
        # the next match has more surface to recognise.
        if name and name != existing.name:
            if existing.name not in existing.aliases:
                existing.aliases.append(existing.name)
            existing.name = name
        if session not in existing.sessions:
            existing.sessions.append(session)
        if body.strip():
            existing.body = body.strip()

    # Highest count first; ties keep the order they were added, so a stable
    # document does not reshuffle itself on every review.
    return sorted(out, key=lambda c: -c.count)


def render(candidates: list[Candidate]) -> str:
    if not candidates:
        return f"{SECTION}\n\n_None yet._\n"
    return f"{SECTION}\n\n" + "\n".join(c.render() for c in candidates)
