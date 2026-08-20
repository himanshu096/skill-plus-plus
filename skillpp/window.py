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

**Windows overlap, and the overlap is the point.** Without it a procedure lying
across a boundary is in no window whole. ``OVERLAP`` of two segments means any
procedure of three or fewer is intact in at least one window. Four or more can
still straddle; that is a real limit, and closing it needs a merge step over
what the windows report rather than a bigger overlap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .extract import extract
from .transcript import Message

# Tokens, approximated as chars/4 -- exact enough to size a window, and it
# avoids a tokeniser dependency in a module that may run beside a local model.
BUDGET = 8_000
OVERLAP = 2
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
    # Which segments this shares with the window before it. Kept so a caller
    # can tell a genuinely new finding from one already reported next door.
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


def windows(messages: list[Message], *, budget: int = BUDGET,
            overlap: int = OVERLAP) -> list[Window]:
    """Fill windows to ``budget`` without ever splitting a segment.

    A segment larger than the budget gets a window to itself rather than being
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
            carried = current.segments[-overlap:] if overlap else []
            current = Window(segments=list(carried), overlapping=len(carried))
        current.segments.append(seg)
    if current.segments and current.segments != (out[-1].segments if out else None):
        out.append(current)
    return out
