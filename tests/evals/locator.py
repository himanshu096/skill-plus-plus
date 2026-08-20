"""Ground truth for the locator question, and how to score an answer.

The question `/locate` asks is per segment and deliberately narrow: did the
work in this request reach a resolved state? Not whether it is worth keeping --
that needs the whole span in working memory and is asked later.

Tested on a frontier model first. If the ceiling cannot answer it, no local
model will, and the architecture that puts this step on a 7B model is dead
before any of it is built.

**The labels are ground truth because these fixtures were written to have one.**
Each was built for a known outcome; the notes below say what makes each answer
the answer, so a disagreement can be argued with rather than just counted.

Two are the ones worth watching.

``their-explore`` segment 0 is ``landed`` and is *also* the case this branch was
measured recording wrongly as a skill. Both are correct: the work did finish,
and it is still not worth keeping. Keeping those two judgements apart is the
whole reason the locator does not ask about worth -- if `landed` quietly meant
"worth capturing", the layer would be reintroducing the mistake it is meant to
isolate.

``recurrence-a`` segments 0 and 1 are ``open``, and they were labelled ``none``
until a run said otherwise three times out of three. Both are reading -- greps,
a spec page, two tracker searches -- and segment 1 is also the first half of the
procedure that finishes in segment 2, which makes it "nothing happened" and
"unresolved" at the same time. Every disagreement the three-verdict version
produced was between those two labels and none involved ``landed``, so the
distinction was costing accuracy and buying nothing: downstream both mean *do
not start a span here*.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# label -> why that label, kept beside it so the fixture can be argued with.
TRUTH: dict[str, list[tuple[str, str]]] = {
    "their-refine": [
        ("open", "edited twice, nothing run — no gate, no commit"),
        ("landed", "tests and lint pass, then committed, and says so"),
    ],
    "their-retry": [
        ("landed", "failed on a lock, retried, pytest passed, committed — a "
                   "failure inside finished work is not abandonment"),
    ],
    "their-distinct": [
        ("landed", "CI file edited, suite green, committed"),
        ("open", "a bare confirmation, no work in it at all"),
        ("landed", "ancestry checked, tag signed, pushed with the commit"),
    ],
    "their-explore": [
        ("landed", "eight greps then an edit then a commit. It finished — and "
                   "is still not worth keeping, which is a different question"),
    ],
    "their-mid-investigation": [
        ("open", "six reads and the agent's own account says nothing is "
                 "conclusive"),
    ],
    "their-recurs": [
        ("landed", "helm upgrade then rollout status reported success"),
        ("open", "a bare confirmation"),
        ("landed", "the same, on the second service"),
        ("open", "a bare confirmation"),
    ],
    "recurrence-a": [
        ("open", "four greps that changed nothing and resolved nothing"),
        ("open", "a spec page and two tracker searches — the first half of the procedure that finishes in segment 2"),
        ("landed", "the issue was updated and the thread posted back, then the "
                   "developer moved on"),
    ],
}

FIXTURES = {
    "recurrence-a": REPO / "tests" / "fixtures" / "recurrence-a.jsonl",
}

VALID = ("landed", "open")
_LINE = re.compile(r"^\s*(\d+)\s+(landed|open)\s*$", re.M | re.I)


def parse(said: str) -> dict[int, str]:
    """The verdicts in a model's reply, whatever else it wrapped them in."""
    return {int(i): v.lower() for i, v in _LINE.findall(said)}


def score(name: str, said: str) -> str | None:
    """Where the answer differs from the truth, or None if it does not.

    Reports every disagreement rather than the first, because the interesting
    failure is usually a pattern -- every read-only segment called work, or
    every unresolved one called finished -- and the first line alone hides it.
    """
    truth = TRUTH[name]
    got = parse(said)
    if not got:
        return f"no verdicts in the reply: {said.strip()[:200]!r}"
    missing = [i for i in range(len(truth)) if i not in got]
    if missing:
        return f"segments not answered: {missing}"
    extra = [i for i in got if i >= len(truth)]
    if extra:
        return f"answered segments that do not exist: {extra}"
    wrong = [f"{i}: said {got[i]}, is {want} ({why})"
             for i, (want, why) in enumerate(truth) if got[i] != want]
    if wrong:
        return f"{len(wrong)}/{len(truth)} wrong — " + "; ".join(wrong)
    return None
