"""Splitting a session into pieces small enough for a local model to read.

Compression cannot get a session under a local context. Measured across twelve
real transcripts, the extract runs 60k-128k tokens *after* a 47x reduction, and
every plausible further cut summed to about 5%. So the prompt has to be split
rather than squeezed -- which is also where small models measured well: one
narrow question over a small window, rather than five questions over everything.

**The unit is a developer request, never a token offset.** A window that ends
mid-request splits a procedure in half, and half a procedure reads as a
finished one -- which is precisely how the capture branch failed, banking
`ruff check` + `git commit` as a recipe of its own because its span closed
early. Boundaries therefore fall only where a person typed something.

**Sizing, measured rather than chosen.** Prompt-to-prompt segments across those
twelve sessions: median 290 tokens, p90 1,458, p95 2,248, p99 4,507. A
procedure is a small number of segments, so a three-segment one costs 4.4k at
p90 and 6.7k at p95. ``BUDGET`` is 8,000 because it clears that with headroom
while leaving room for the instructions and the store beside it in a 16k
context, and because only 3 of 1,758 real segments exceed it alone.

**Windows overlap, and the overlap is measured in tokens, not segments.**
Without any overlap a procedure lying across a boundary is in no window whole.
Carrying back a *count* of segments was the obvious way to fix that and is the
wrong one: two segments out of an average nine duplicated 48% of all content
across eight real sessions, because the segments at the end of a full window
are the large ones.

Carrying back a token budget instead costs 12% for the same job, and covers
*more* segments while doing it -- 11.5 per window against 9.6 -- because it
picks up a run of small segments rather than two big ones. ``OVERLAP`` is 1,500
tokens, which is p90 of a single segment, so the earlier part of a straddling
procedure is carried in nine cases out of ten.

    segment count 2   163 windows   48.1% duplicated
    token cap 2,500   155 windows   27.1%
    token cap 1,500   134 windows   12.0%   <- OVERLAP
    token cap 800     125 windows    4.0%

What no overlap can fix, and all three want a merge over what the windows
report rather than a larger carry:

- A procedure longer than the carry is intact in no window.
- A window can hold a procedure's tail without its method.
- The carry has to stay contiguous with the boundary, so a boundary whose last
  segment is larger than the carry carries nothing at all. That is the p90
  segment, so roughly one boundary in ten has no overlap protection.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .extract import extract
from .transcript import Message

# Tokens, approximated as chars/4 -- exact enough to size a window, and it
# avoids a tokeniser dependency in a module that may run beside a local model.
BUDGET = 8_000
OVERLAP = 1_500  # tokens carried into the next window, not a segment count
CHARS_PER_TOKEN = 4


@dataclass
class Segment:
    """One developer request and the work that followed it."""

    messages: list[Message]
    text: str

    @property
    def tokens(self) -> int:
        return len(self.text) // CHARS_PER_TOKEN

    @property
    def ask(self) -> str:
        """The request itself, for naming a window in a report."""
        for line in self.text.splitlines():
            if line.strip().startswith("> "):
                return line.strip()[2:][:80]
        return ""


@dataclass
class Window:
    """A run of whole segments, small enough to be read in one call."""

    segments: list[Segment] = field(default_factory=list)
    # How many leading segments this shares with the window before it. Kept so
    # a caller can tell a genuinely new finding from one already reported next
    # door -- which it must do, since the same procedure is deliberately shown
    # to more than one window.
    overlapping: int = 0

    @property
    def text(self) -> str:
        return "\n".join(s.text for s in self.segments)

    @property
    def tokens(self) -> int:
        return sum(s.tokens for s in self.segments)


# User-role messages the harness injected rather than a person sending them.
# Measured at 1.2% of segment boundaries across eight real sessions -- small,
# but a task notification arriving mid-procedure breaks the segment at exactly
# the wrong place, and the resulting half is what gets read as finished work.
_INJECTED = ("<task-notification>", "<system-reminder>", "<local-command",
             "<command-name>", "<command-message>", "<command-args>")


def _is_prompt(message: Message) -> bool:
    """A person typed this, rather than a tool or the harness speaking.

    A user-role message carrying tool_result blocks is the transcript's way of
    returning output, not a new request -- treating one as a boundary would put
    a segment break between every call and its own result.
    """
    if message.role != "user":
        return False
    if any(b.kind == "tool_result" for b in message.blocks):
        return False
    text = message.text.lstrip()
    return not any(text.startswith(marker) for marker in _INJECTED)


def segments(messages: list[Message]) -> list[Segment]:
    """Split at developer prompts, keeping each request with its own work.

    Anything before the first prompt is its own segment rather than being
    discarded: a resumed conversation opens mid-work, and that work is real.
    """
    groups: list[list[Message]] = []
    for message in messages:
        if _is_prompt(message) and groups:
            groups.append([message])
        elif groups:
            groups[-1].append(message)
        else:
            groups.append([message])
    out = []
    for group in groups:
        text = extract(group).strip()
        if text:
            out.append(Segment(messages=group, text=text))
    return out


def _carry(done: list[Segment], overlap: int) -> list[Segment]:
    """The tail of a finished window, up to ``overlap`` tokens of it.

    Taken from the end backwards and never partially: half a segment carried
    forward is half a request, which is the thing this module exists to avoid.
    """
    if overlap <= 0:
        return []
    carried: list[Segment] = []
    total = 0
    for seg in reversed(done):
        if total + seg.tokens > overlap:
            break
        carried.insert(0, seg)
        total += seg.tokens
    return carried


def windows(messages: list[Message], *, budget: int = BUDGET,
            overlap: int = OVERLAP) -> list[Window]:
    """Fill windows to ``budget`` without ever splitting a segment.

    ``overlap`` is a token budget carried into the next window, not a number of
    segments -- see the module docstring for why that distinction is worth 36
    percentage points of duplicated input.

    A segment larger than ``budget`` gets a window to itself rather than being
    cut: 3 of 1,758 measured segments were, the largest 11,940 tokens, and
    cutting one would split the request it represents from its own work.
    """
    segs = segments(messages)
    if not segs:
        return []
    out: list[Window] = []
    current = Window()
    for seg in segs:
        if current.segments and current.tokens + seg.tokens > budget:
            out.append(current)
            carried = _carry(current.segments, overlap)
            current = Window(segments=list(carried), overlapping=len(carried))
        current.segments.append(seg)
    if current.segments and current.segments != (out[-1].segments if out else None):
        out.append(current)
    return out
