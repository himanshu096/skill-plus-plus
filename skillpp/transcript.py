"""Reading a Claude Code session transcript.

This is the only module that touches the transcript's internals, and it exists
to keep it that way. The hook payload's ``transcript_path`` is documented; the
shape of the JSONL it points at is not. Field names like ``isSidechain`` and the
block structure inside ``message.content`` are implementation details that can
change without notice.

So everything downstream reads :class:`Message` and :class:`Block` instead of
raw records. When the format does change, one module and one set of tests break
loudly, rather than four modules quietly producing empty summaries.

That loudness is deliberate: a transcript holding records but no recognisable
messages raises :class:`TranscriptFormatError` rather than returning ``[]``.
Checked against every transcript on this machine, the two are never confused —
a file with records always has messages — so an empty result from a non-empty
file means the format moved, and silently summarising nothing would hide it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Record types that carry the conversation. Everything else in the file is
# bookkeeping — attachments, turn timings, title guesses, permission modes.
CONVERSATION_TYPES = ("user", "assistant")


class TranscriptFormatError(RuntimeError):
    """The transcript did not look like a transcript.

    Raised only when a file holds parseable records but none of them are
    conversation messages, which means the format changed underneath us.
    """


@dataclass(frozen=True)
class Block:
    """One piece of a message's content.

    A single class with a ``kind`` tag rather than a class per block type: the
    consumers all switch on the kind anyway, and this keeps the normalising in
    one place instead of spreading ``isinstance`` chains downstream.
    """

    kind: str  # text | tool_use | tool_result | thinking | other
    text: str = ""
    name: str = ""  # tool_use: the tool's name
    tool_input: dict = field(default_factory=dict)
    tool_use_id: str = ""  # tool_use: its own id. tool_result: the call's id.
    is_error: bool = False  # tool_result only


@dataclass(frozen=True)
class Message:
    """One conversation turn, stripped of everything not needed downstream."""

    uuid: str
    role: str
    timestamp: str  # raw ISO-8601, parsed only where a comparison needs it
    blocks: tuple[Block, ...]

    @property
    def text(self) -> str:
        """All plain text in this message, joined. Empty for tool-only turns."""
        return "\n".join(b.text for b in self.blocks if b.kind == "text" and b.text)


def _result_text(raw: object) -> str:
    """Flatten a tool result, which is sometimes a string and sometimes blocks."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = [b.get("text", "") for b in raw if isinstance(b, dict)]
        return "\n".join(p for p in parts if p)
    return "" if raw is None else str(raw)


def _blocks(content: object) -> tuple[Block, ...]:
    """Normalise a message's content into blocks.

    A user message is often a bare string; an assistant message is always a
    list. Both arrive here and leave in the same shape.
    """
    if isinstance(content, str):
        return (Block(kind="text", text=content),) if content else ()
    if not isinstance(content, list):
        return ()

    out: list[Block] = []
    for raw in content:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type", "")
        if kind == "text":
            out.append(Block(kind="text", text=raw.get("text", "")))
        elif kind == "thinking":
            out.append(Block(kind="thinking", text=raw.get("thinking", "")))
        elif kind == "tool_use":
            out.append(Block(
                kind="tool_use",
                name=raw.get("name", ""),
                tool_input=raw.get("input") or {},
                tool_use_id=raw.get("id", ""),
            ))
        elif kind == "tool_result":
            out.append(Block(
                kind="tool_result",
                text=_result_text(raw.get("content")),
                tool_use_id=raw.get("tool_use_id", ""),
                # Absent means success: only failures are flagged.
                is_error=bool(raw.get("is_error")),
            ))
        else:
            out.append(Block(kind="other", text=str(kind)))
    return tuple(out)


def read(path: str | Path) -> list[Message]:
    """Conversation messages from a transcript, in file order.

    Skips sidechain records — those are subagent traffic, not the developer's
    conversation — and tolerates a torn final line, which is normal because the
    file is written asynchronously and may be mid-flush when it is read.
    """
    path = Path(path)
    messages: list[Message] = []
    records = 0

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A partially-flushed last line is expected, not exceptional.
                continue
            if not isinstance(record, dict):
                continue
            records += 1

            if record.get("type") not in CONVERSATION_TYPES:
                continue
            if record.get("isSidechain"):
                continue
            uuid = record.get("uuid")
            if not uuid:
                continue

            message = record.get("message") or {}
            messages.append(Message(
                uuid=str(uuid),
                role=str(message.get("role") or record.get("type")),
                timestamp=str(record.get("timestamp") or ""),
                blocks=_blocks(message.get("content")),
            ))

    if records and not messages:
        raise TranscriptFormatError(
            f"{path}: {records} record(s) but no conversation messages. "
            f"The transcript format has probably changed — this module needs "
            f"updating rather than the caller working around it."
        )
    return messages
