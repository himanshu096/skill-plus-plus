"""The candidate store: parsing, updating, rendering.

Everything here is bookkeeping, and it exists so that a model never has to do
it. An earlier design had the model rewrite the whole document on each review —
carrying every existing candidate forward by hand, incrementing counts, keeping
the order. That works until it doesn't, and when it doesn't the failure is a
candidate quietly missing from a file that may hold months of them.

So the model is left with the one question it is actually needed for: *does
this proposal describe a procedure already in here, or a new one?* It answers
with a name and a body. Counting, provenance, ordering, status and rewriting
happen here, where they cannot be forgotten.

Two axes, deliberately separate. **Status** decides which section an entry
lives in and only ever changes because a person decided something. **Evidence**
— how often it has been seen — orders entries within a section and never
promotes anything on its own. Conflating them is what made an earlier design
rank "run the test suite" above "file a ticket the team's way".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

CANDIDATE = "candidate"
PROMOTED = "promoted"

SECTIONS = {
    CANDIDATE: "## Skill candidates",
    PROMOTED: "## Made into skills",
}

HEADING = re.compile(r"^###\s+(.+?)\s*$", re.M)
SECTION_HEAD = re.compile(r"^##\s+(.+?)\s*$", re.M)
SEEN = re.compile(r"^\*\*seen\s+(\d+)×\*\*\s*(?:·\s*(.*))?$", re.M)
ALIAS = re.compile(r"^also seen as:\s*(.*)$", re.M)
DATES = re.compile(r"^first\s+(\S+)\s*·\s*last\s+(\S+)\s*$", re.M)
SKILL = re.compile(r"^skill:\s*(.*)$", re.M)
PROMOTED_ON = re.compile(r"^promoted\s+(\S+)\s*$", re.M)

_META = (SEEN, ALIAS, DATES, SKILL, PROMOTED_ON)


@dataclass
class Candidate:
    """One proposed skill, the evidence for it, and what was decided."""

    name: str
    body: str = ""
    sessions: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    status: str = CANDIDATE
    first_seen: str = ""
    last_seen: str = ""
    promoted_on: str = ""
    skill_path: str = ""

    @property
    def count(self) -> int:
        """How many times this has been seen.

        Derived from the provenance rather than stored beside it, so the two
        cannot drift apart.
        """
        return len(self.sessions)

    def render(self) -> str:
        lines = [f"### {self.name}",
                 f"**seen {self.count}×** · " + ", ".join(self.sessions)]
        if self.first_seen or self.last_seen:
            lines.append(f"first {self.first_seen or '?'} · last {self.last_seen or '?'}")
        if self.aliases:
            lines.append("also seen as: " + ", ".join(f'"{a}"' for a in self.aliases))
        if self.status == PROMOTED:
            if self.promoted_on:
                lines.append(f"promoted {self.promoted_on}")
            if self.skill_path:
                lines.append(f"skill: {self.skill_path}")
        return "\n".join(lines) + "\n\n" + self.body.strip() + "\n"


def _status_of(heading: str) -> str:
    return PROMOTED if "made into skills" in heading.lower() else CANDIDATE


def parse(document: str) -> list[Candidate]:
    """Read the candidates out of the store.

    Tolerant by design: a hand-edited document should still load. A heading
    with no `seen` line is a candidate seen zero times, not a parse error.
    """
    out: list[Candidate] = []
    sections = list(SECTION_HEAD.finditer(document))
    entries = list(HEADING.finditer(document))

    for index, match in enumerate(entries):
        # An entry ends at the next entry *or* the next section heading,
        # whichever comes first. Without the section, the last entry of a
        # group swallows the heading that follows it.
        bounds = [e.start() for e in entries[index + 1:]]
        bounds += [s.start() for s in sections if s.start() > match.start()]
        end = min(bounds) if bounds else len(document)
        block = document[match.end():end]
        # Whichever `##` heading most recently preceded this entry.
        owner = [s for s in sections if s.start() < match.start()]
        status = _status_of(owner[-1].group(1)) if owner else CANDIDATE

        sessions: list[str] = []
        seen = SEEN.search(block)
        if seen and seen.group(2):
            sessions = [s.strip() for s in seen.group(2).split(",") if s.strip()]

        aliases: list[str] = []
        alias = ALIAS.search(block)
        if alias:
            aliases = [a.strip().strip('"') for a in alias.group(1).split(",") if a.strip()]

        dates = DATES.search(block)
        first, last = (dates.group(1), dates.group(2)) if dates else ("", "")
        first = "" if first == "?" else first
        last = "" if last == "?" else last

        promoted = PROMOTED_ON.search(block)
        skill = SKILL.search(block)

        body = block
        for pattern in _META:
            found = pattern.search(body)
            if found:
                body = body[:found.start()] + body[found.end():]

        out.append(Candidate(
            name=match.group(1).strip(),
            body=body.strip(),
            sessions=sessions,
            aliases=aliases,
            status=status,
            first_seen=first,
            last_seen=last,
            promoted_on=promoted.group(1) if promoted else "",
            skill_path=skill.group(1).strip() if skill else "",
        ))
    return out


def find(candidates: list[Candidate], name: str) -> Candidate | None:
    """A candidate by name or by a name it was previously known as."""
    wanted = name.strip().lower()
    return next((c for c in candidates
                 if c.name.lower() == wanted
                 or wanted in (a.lower() for a in c.aliases)), None)


def upsert(candidates: list[Candidate], *, name: str, body: str, session: str,
           matches: str | None = None, today: str | None = None) -> list[Candidate]:
    """Fold one proposal in, and return the list in reading order.

    ``matches`` names an existing entry the model judged to be the same
    procedure. Everything that follows from that — the count, the provenance,
    the dates, recording the old name when it changes — is decided here rather
    than written out by the model.

    A proposal matching something already promoted keeps its status. The
    procedure recurring is evidence the skill earns its place, not a reason to
    put it back in the queue.
    """
    out = list(candidates)
    stamp = today or date.today().isoformat()
    existing = None
    if matches:
        existing = find(out, matches)
        if existing is None:
            raise KeyError(
                f"no candidate named {matches!r} to match against; "
                f"have: {', '.join(c.name for c in out) or '(none)'}")

    if existing is None:
        out.append(Candidate(name=name, body=body, sessions=[session],
                             first_seen=stamp, last_seen=stamp))
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
        existing.first_seen = existing.first_seen or stamp
        existing.last_seen = stamp

    return order(out)


def promote(candidates: list[Candidate], name: str, *, skill_path: str,
            today: str | None = None) -> list[Candidate]:
    """Mark one candidate as having become a skill.

    Deterministic and one-way: the decision is a person's, everything that
    records it is not. Promoting does not touch the evidence — the count and
    provenance stay, because they are why it was promoted.
    """
    out = list(candidates)
    entry = find(out, name)
    if entry is None:
        raise KeyError(
            f"no candidate named {name!r}; "
            f"have: {', '.join(c.name for c in out) or '(none)'}")
    if entry.status == PROMOTED:
        raise ValueError(f"{entry.name!r} is already a skill: {entry.skill_path}")
    entry.status = PROMOTED
    entry.promoted_on = today or date.today().isoformat()
    entry.skill_path = skill_path
    return order(out)


def order(candidates: list[Candidate]) -> list[Candidate]:
    """Reading order: most evidence first, within a stable section order."""
    return sorted(candidates, key=lambda c: (c.status != CANDIDATE, -c.count))


def render(candidates: list[Candidate]) -> str:
    parts = []
    for status in (CANDIDATE, PROMOTED):
        group = [c for c in order(candidates) if c.status == status]
        if status == PROMOTED and not group:
            continue  # an empty section here is noise, not information
        parts.append(SECTIONS[status] + "\n\n" +
                     ("\n".join(c.render() for c in group) if group
                      else "_None yet._\n"))
    return "\n".join(parts)
