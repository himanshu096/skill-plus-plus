"""Which conversation a transcript belongs to.

Claude Code mints a new session id every time a conversation is resumed, and
writes a new transcript file containing the whole history again. Keying memory
on the session id therefore produces one file per resume — each a superset of
the last, with the occurrence counts split across all of them.

The conversation's *first* message survives every resume, because resuming
copies the history forward. So the first message's uuid is the identity.

**Why only the first message.** An earlier version hashed the first five uuids,
on the theory that more of them meant more confidence. Measured against every
transcript on this machine, that is backwards: uuids are already unique, so
extra ones add no certainty, and they add a way to fail. A conversation that is
rewound diverges from its earlier snapshot at some point, and any prefix longer
than that point yields a different id — splitting one conversation into two
memory files, silently. One real conversation diverged at message index 2, so
five split it and one did not. One is the shortest possible prefix and the only
one that cannot be broken by an early rewind.

The uuid is kept recognisable rather than hashed: an id you can grep for in
`~/.claude/projects/` is worth more than an opaque one, and hashing would buy
nothing since the uuid is already opaque, unique and fixed-length.

Known limitation — accepted, not solved
---------------------------------------
Resting identity on a single message leaves no redundancy. Two failure modes
follow. Neither has been observed here, but both are silent, so they are
recorded for whoever finds a memory file that looks wrong.

*Two conversations sharing an id* needs two transcripts whose first message
uuid is identical. Uuids are generated to prevent exactly that, so it should
not happen — but the symptom would be one memory document describing work that
never happened together.

*One conversation splitting into two ids* needs the first message itself to be
rewritten between snapshots. Turn regeneration is routine — one conversation
here diverged at message index 2 — but it has never been seen to touch the
opening message. If it did, no prefix length would survive it. The symptom is a
second memory file appearing for a conversation that already had one, with the
counts split across both.

Neither is detectable from a single run, which is why this is a note rather
than a guard: the check would cost more than the failure, and the failure is
legible once you know to look for it.
"""

from __future__ import annotations

from .transcript import Message

# Enough of the uuid to stay unique at any plausible scale, short enough to
# read in a filename. 16 hex characters is 64 bits.
ID_CHARS = 16


class NoConversationError(ValueError):
    """A transcript with no messages has no conversation to identify."""


def conversation_id(messages: list[Message]) -> str:
    """The id of the conversation these messages belong to.

    Identical for every snapshot of one conversation, including snapshots whose
    tail was rewritten on resume and branches created by rewinding.
    """
    if not messages:
        raise NoConversationError(
            "cannot identify a conversation from an empty transcript")
    return messages[0].uuid.replace("-", "")[:ID_CHARS]
