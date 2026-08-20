"""Turning per-window findings back into one answer about the session.

Windowing buys a prompt a local model can read and creates three problems, all
of which land here rather than in `window.py`:

- The overlap deliberately shows the same procedure to two windows, so the same
  finding arrives twice and must not be counted twice.
- A procedure longer than the carry is intact in no window, so it arrives as two
  partial findings that must become one.
- A window can hold a procedure's tail without its method, so a finding can be
  real and still be half of something.

The first pulls against the second: dedupe too eagerly and a split procedure's
two halves collapse into one half; merge too eagerly and two genuinely different
procedures become one entry, which is the silent failure -- it inflates a count
and discards a proposal with nothing recording that it happened.

**Provenance decides, not prose.** Every finding says which segments it drew on,
and that is enough to separate the cases mechanically. Only what is genuinely
ambiguous reaches a model, which is the whole point: the count of judgement
calls is bounded by real ambiguity rather than by the number of windows.

The rule that is easy to get wrong: *the same name over disjoint segments is not
a duplicate, it is a recurrence.* A procedure done twice in one session is two
sightings of one entry -- exactly what `their-recurs` tests -- and folding those
into one because the names match destroys the evidence the entry earns its
place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A group's verdict, and who decides it.
DUPLICATE = "duplicate"    # code: same work, seen twice because windows overlap
RECURRENCE = "recurrence"  # code: same procedure, genuinely done more than once
DISTINCT = "distinct"      # code: different work, far apart
SPLIT = "split?"           # model: adjacent halves, possibly one procedure
AMBIGUOUS = "ambiguous?"   # model: same segments, different names


@dataclass
class Finding:
    """One procedure a window reported, and what it was looking at."""

    name: str
    body: str
    window: int
    span: tuple[int, int]
    # The reporter's own claim that it saw the whole thing. A window holding a
    # procedure's tail without its method should say so; a False here is a
    # reason to look for the other half rather than a reason to discard.
    complete: bool = True

    @property
    def width(self) -> int:
        return self.span[1] - self.span[0] + 1


@dataclass
class Group:
    """Findings that provenance says are about the same stretch of work."""

    findings: list[Finding] = field(default_factory=list)
    verdict: str = DISTINCT

    @property
    def span(self) -> tuple[int, int]:
        return (min(f.span[0] for f in self.findings),
                max(f.span[1] for f in self.findings))

    @property
    def best(self) -> Finding:
        """The finding that saw the most, preferring one that claims to be whole.

        Where two windows report the same procedure, the one whose span is wider
        read more of it -- and a body written from more of the procedure is the
        one worth keeping.
        """
        return max(self.findings, key=lambda f: (f.complete, f.width))

    @property
    def needs_judgement(self) -> bool:
        return self.verdict in (SPLIT, AMBIGUOUS)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def _adjacent(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Touching without overlapping -- one ends where the other begins."""
    return a[1] + 1 == b[0] or b[1] + 1 == a[0]


def _same(a: str, b: str) -> bool:
    """Whether two proposals are calling the same thing by the same name.

    Deliberately exact after normalising separators, not fuzzy. A lexical
    similarity score was measured on this problem and returned 0.00 on every
    real pair of names for the same procedure, because the same work gets named
    differently every time. Anything softer than equality is a judgement, and
    judgements are routed to a model instead of guessed at here.
    """
    return a.strip().lower().replace("_", "-") == b.strip().lower().replace("_", "-")


def _verdict(a: Finding, b: Finding) -> str:
    if _overlaps(a.span, b.span):
        return DUPLICATE if _same(a.name, b.name) else AMBIGUOUS
    if _adjacent(a.span, b.span):
        # Same name over adjacent spans is the one case adjacency does not make
        # suspicious: doing the same procedure twice back to back is a
        # recurrence, not a procedure cut in half.
        if _same(a.name, b.name):
            return RECURRENCE
        return SPLIT
    return RECURRENCE if _same(a.name, b.name) else DISTINCT


def reconcile(findings: list[Finding]) -> list[Group]:
    """Group findings across windows, and say which groups need a judgement.

    Groups are built transitively: if A relates to B and B to C, all three are
    one group, because a procedure spread over three windows arrives as three
    findings that pairwise touch. A group's verdict is the least settled of its
    pairs -- one ambiguous pair makes the group a question, since resolving it
    is what decides whether the rest belongs together.
    """
    ordered = sorted(findings, key=lambda f: (f.span[0], f.span[1], f.name))
    groups: list[Group] = []
    for finding in ordered:
        joined: list[Group] = []
        for group in groups:
            for member in group.findings:
                if _verdict(finding, member) != DISTINCT:
                    joined.append(group)
                    break
        if not joined:
            groups.append(Group(findings=[finding]))
            continue
        # A finding can bridge two groups that were separate until now.
        first = joined[0]
        first.findings.append(finding)
        for other in joined[1:]:
            first.findings.extend(other.findings)
            groups.remove(other)

    # Rank so the least settled wins: a group holding one ambiguous pair is a
    # question however many of its other pairs are clear.
    order = [AMBIGUOUS, SPLIT, DUPLICATE, RECURRENCE, DISTINCT]
    for group in groups:
        verdicts = {_verdict(a, b)
                    for i, a in enumerate(group.findings)
                    for b in group.findings[i + 1:]}
        group.verdict = next((v for v in order if v in verdicts), DISTINCT)
    return sorted(groups, key=lambda g: g.span)


def questions(groups: list[Group]) -> list[Group]:
    """The groups a model has to look at. Everything else is already decided."""
    return [g for g in groups if g.needs_judgement]


def settled(groups: list[Group]) -> list[Finding]:
    """One finding per group that needed no judgement.

    A ``RECURRENCE`` group is deliberately *not* collapsed to one finding here:
    each member is its own sighting, and the count depends on them staying
    separate.
    """
    out: list[Finding] = []
    for group in groups:
        if group.needs_judgement:
            continue
        if group.verdict == RECURRENCE:
            out.extend(group.findings)
        else:
            out.append(group.best)
    return out
