"""The candidate store: entry files plus two append-only logs.

Everything here is bookkeeping, and it exists so that a model never has to do
it. The model is left with the one question it is actually needed for: *does
this proposal describe a procedure already in here, or a new one?* It answers
with a name and a body. Counting, provenance, ordering and status happen here.

**Nothing is ever rewritten.** A body is written once, to its own file. A match
appends one line to a log. That is not a convention to be careful about; it is
the shape of the store, so the failure it replaces cannot recur.

What it replaces, and why: the store used to be one markdown document whose
parser ended an entry at the next ``##`` heading. Skill bodies use ``##`` for
their own rules, so every body was silently cut at its first rule — and since a
write re-serialised the whole document, recording against one entry destroyed
the bodies of entries nobody had touched. Measured, not theorised.

Two axes, deliberately separate. **Status** comes from the last recorded
decision and only ever changes because a person decided something. **Evidence**
— how often it has been seen — orders entries and never promotes anything on
its own. Conflating them is what made an earlier design rank "run the test
suite" above "file a ticket the team's way".

Everything countable is **derived** rather than stored, so no two records of the
same fact can drift: the count is the number of logged sightings, the dates are
their range, the status is the last decision.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import Config
from .lifecycle import parse_frontmatter

# Anchored to the start of the file and stopping at the first close, so a
# `---` horizontal rule inside a body is content rather than a delimiter.
# Matching anywhere -- or taking the last split -- is the mistake that cut
# every body at its first `##` in the store this replaces.
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)

CANDIDATE = "candidate"
PROMOTED = "promoted"

# Below this a recorded procedure is an observation, not a proposal. One
# sighting is a thing that happened; two is a coincidence. Putting either in
# front of a person spends the only attention the store gets on work that has
# not yet shown it recurs -- and a queue full of those stops being read at all.
THRESHOLD = 3


@dataclass
class Candidate:
    """One procedure, assembled from its entry file and the logs."""

    name: str
    body: str = ""
    sessions: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    status: str = CANDIDATE
    skill_path: str = ""
    promoted_on: str = ""
    path: Path | None = None

    @property
    def count(self) -> int:
        """How many times this has been seen.

        Derived from the provenance rather than stored beside it, so the two
        cannot drift apart.
        """
        return len(self.sessions)

    @property
    def first_seen(self) -> str:
        return min(self.dates) if self.dates else ""

    @property
    def last_seen(self) -> str:
        return max(self.dates) if self.dates else ""


def _slug(name: str) -> str:
    """A filename that cannot escape the store directory."""
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in name.strip()]
    return "".join(keep).strip("-") or "unnamed"


def entry_path(config: Config, name: str) -> Path:
    return config.patterns_dir / f"{_slug(name)}.md"


def add_entry(config: Config, *, name: str, body: str) -> Path:
    """Write a new entry. Refuses to touch one that already exists.

    Refusing is the point: an entry file is the one copy of what a procedure
    is, and overwriting it is how the old store lost bodies.
    """
    config.patterns_dir.mkdir(parents=True, exist_ok=True)
    path = entry_path(config, name)
    if path.exists():
        raise FileExistsError(
            f"{path} already exists; a match appends an occurrence instead of "
            f"rewriting the body")
    path.write_text(f"---\nname: {name}\n---\n\n{body.strip()}\n", encoding="utf-8")
    return path


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def add_occurrence(config: Config, *, name: str, session: str,
                   today: str | None = None) -> None:
    """Record one sighting. The only thing a match writes."""
    _append(config.occurrences_file,
            {"name": name, "session": session,
             "date": today or date.today().isoformat()})


def record_decision(config: Config, *, name: str, action: str,
                    skill_path: str = "", today: str | None = None) -> None:
    """Record what a person decided. The only thing that changes status."""
    _append(config.decisions_file,
            {"name": name, "action": action, "skill_path": skill_path,
             "date": today or date.today().isoformat()})


def _read_log(path: Path) -> list[dict]:
    """Tolerant by design: a truncated final line loses that line, not the log."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("name"):
            out.append(record)
    return out


def load(config: Config) -> list[Candidate]:
    """Assemble every candidate from its file and the logs, in reading order."""
    entries: dict[str, Candidate] = {}
    if config.patterns_dir.exists():
        for path in sorted(config.patterns_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            front = parse_frontmatter(text)
            found = _FRONTMATTER.match(text)
            body = text[found.end():].strip() if found else text.strip()
            name = str(front.get("name") or path.stem)
            entries[name] = Candidate(name=name, body=body, path=path)

    for record in _read_log(config.occurrences_file):
        entry = entries.get(record["name"])
        if entry is None:
            continue
        session = str(record.get("session") or "")
        if session and session not in entry.sessions:
            entry.sessions.append(session)
            entry.dates.append(str(record.get("date") or ""))

    for record in _read_log(config.decisions_file):
        entry = entries.get(record["name"])
        if entry is None:
            continue
        # Last decision wins, so promotion history survives in the log while
        # the current state stays unambiguous.
        entry.status = str(record.get("action") or CANDIDATE)
        entry.skill_path = str(record.get("skill_path") or "")
        entry.promoted_on = str(record.get("date") or "")

    return order(list(entries.values()))


def find(candidates: list[Candidate], name: str) -> Candidate | None:
    """By name, then by slug, so a renamed-on-disk entry is still reachable."""
    for candidate in candidates:
        if candidate.name == name:
            return candidate
    wanted = _slug(name)
    for candidate in candidates:
        if _slug(candidate.name) == wanted:
            return candidate
    return None


def reviewable(candidates: list[Candidate],
               threshold: int = THRESHOLD) -> list[Candidate]:
    """The ones worth asking a person about.

    This filters the queue; it does not change what an entry *is*. Everything
    below the threshold stays stored, stays counted, and still shows up in
    ``skillpp candidates`` -- it is simply not a question yet. Keeping the two
    apart is the same separation as status and evidence: what a thing is, and
    how much of it there is, are not the same fact.
    """
    return [c for c in candidates
            if c.status == CANDIDATE and c.count >= threshold]


def order(candidates: list[Candidate]) -> list[Candidate]:
    """Candidates before promoted, most-seen first inside each group.

    The count does not decide whether an entry belongs; a person's decision
    did that. It decides what gets read first.
    """
    return sorted(candidates, key=lambda c: (c.status != CANDIDATE, -c.count, c.name))


def render_entry(candidate: Candidate) -> str:
    """One entry as the model is shown it: provenance, then the body verbatim."""
    head = [f"### {candidate.name}",
            f"**seen {candidate.count}×** · " + ", ".join(candidate.sessions)]
    if candidate.dates:
        head.append(f"first {candidate.first_seen} · last {candidate.last_seen}")
    if candidate.status != CANDIDATE:
        head.append(f"{candidate.status} {candidate.promoted_on}".strip())
    if candidate.skill_path:
        head.append(f"skill: {candidate.skill_path}")
    return "\n".join(head) + "\n\n" + candidate.body.strip() + "\n"


def render(candidates: list[Candidate]) -> str:
    """Every candidate, grouped, for a prompt or for reading.

    Derived on demand. There is no rendered document on disk to fall out of
    step with the entry files, and nothing reads this back.
    """
    groups = ((CANDIDATE, "## Skill candidates"), (PROMOTED, "## Made into skills"))
    parts: list[str] = []
    for status, heading in groups:
        picked = [c for c in candidates if (c.status == CANDIDATE) == (status == CANDIDATE)]
        parts.append(heading + "\n")
        parts.append("\n".join(render_entry(c) for c in picked) if picked
                     else "_None yet._\n")
    return "\n".join(parts).strip() + "\n"
