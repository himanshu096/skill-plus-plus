"""Compressing a stretch of conversation into what is worth reading.

A transcript is roughly 99% noise by volume, and the signal is close to
constant regardless of size. Measured across every transcript on this machine,
this reduces the corpus by about 40x with the reasoning intact.

Two decisions here were made by measurement rather than taste, and both are
easy to get wrong in the obvious direction.

**Tool results are kept, selectively.** The tempting rule is to drop them all:
they are most of the bytes, and the commands alone look like enough. They are
not. Checked against a summary written from a full transcript, dropping results
lost almost every concrete claim it made -- which binaries were missing, that
the test suite passed, what the coverage was. Those are what a summary's
judgement is built from. So results are kept for tools whose outcome changed
the course of the session, and dropped for tools where it did not: the last few
lines of a file read say nothing, while a failed command says everything.
Failures are kept regardless of tool.

**The dialogue is kept, not just the actions.** An earlier version kept
assistant prose only where a short reply followed it, on the theory that the
tool calls carried the work. Measured, that preserved 1.7% of what the agent
said -- and some of the practices most worth capturing live entirely in that
1.7%. Proposing a plan and inviting the developer to challenge it, or asking
for scope and limits before touching code, produce no tool call at all. The
questions asked and the plans proposed are rendered in full for the same
reason; asking before acting is itself the thing being recognised.

Also removed: an expanded slash command, which arrives as a user message
carrying the command's entire definition. One `/log-session` contributed about
5KB to an 8KB extract -- most of the file was the instructions for writing a
summary rather than anything that happened.
"""

from __future__ import annotations

import re

from .transcript import Message

# Which input fields identify what a call actually did. Anything not listed
# falls back to the first couple of scalar arguments.
ARG_KEYS: dict[str, tuple[str, ...]] = {
    "Bash": ("command",),
    "Read": ("file_path",),
    # What a change said is as much the work as which file it touched. Dropping
    # it was measured to cost more than any other omission: in a session spent
    # documenting a module, every function name lived in these fields, and a
    # summary written from the extract could not name a single one.
    "Edit": ("file_path", "new_string"),
    "Write": ("file_path", "content"),
    "Glob": ("pattern", "path"),
    "Grep": ("pattern", "path"),
    "Task": ("description",),
    "Skill": ("skill", "args"),
    "WebFetch": ("url",),
    "WebSearch": ("query",),
    "TodoWrite": (),
    # Rendered specially below: their arguments are the content, not a label.
    "AskUserQuestion": (),
    "ExitPlanMode": (),
}


def _questions(tool_input: dict) -> str:
    """The questions asked, with their options.

    Asking before acting is itself a practice worth recognising, and the
    options offered are what a later "3" or "yes" in the transcript refers to.
    """
    out = []
    for question in tool_input.get("questions") or []:
        if not isinstance(question, dict):
            continue
        out.append(question.get("question", ""))
        labels = [o.get("label", "") for o in question.get("options") or []
                  if isinstance(o, dict)]
        if labels:
            out.append("      options: " + " | ".join(labels))
    return "\n    ".join(p for p in out if p)

# Tools whose result is worth keeping. The test is not "did it change
# something" but "could this be recovered later?" -- a local Read returns a
# file that is still sitting in the repo, so its output is redundant. A
# Confluence page, a tracker query or a web fetch returns state from outside
# the repo, which nothing downstream can recover.
#
# The distinction was missed at first because the corpus this was tuned on used
# MCP only for browser automation, where results really are noise. In a session
# that files a ticket by reading a spec page, the MCP result *is* the content:
# dropping it left the tool calls with the whole procedure missing.
OUTCOME_TOOLS = frozenset({"Bash", "Edit", "Write", "NotebookEdit", "Task", "Skill"})


def _keeps_outcome(name: str) -> bool:
    return (name in OUTCOME_TOOLS
            or name.startswith("mcp__")
            or name in ("WebFetch", "WebSearch"))

# How much of a call's arguments to keep, per tool. A single number across all
# tools was wrong by a wide margin: measured on this corpus, one cap of 300
# truncated 42% of every call made -- 69% of Edits and 98% of Writes, whose
# arguments *are* the change being made. Median argument length differs 25x
# between Read (a path) and Write (a file), so the cap has to differ too.
# The split that matters is not tool by tool but what the argument *is*.
#
# For some tools the argument is the step: `git merge --ff-only <branch>`, a
# JIRA filter, a Confluence page id. Remove it and the procedure is gone. These
# get room.
#
# For others the argument is the *product* -- the content Write wrote, the text
# Edit inserted. The step is "wrote the file"; the content is what came out of
# the procedure, not how it ran. These get a path and little else. Keeping them
# was measured to cost 4 MB corpus-wide and push 13 more sessions over budget,
# for content that answers none of intent, steps, outcome or generalizability.
ARG_CHARS: dict[str, int] = {
    "Bash": 1500,      # p90 is 851; heredocs carry conventions worth keeping
    "Task": 1200,      # the instruction given to a subagent is the step
    "Edit": 250,       # path plus a glimpse; the diff is the product
    "Write": 250,
    "Skill": 800,
}
DEFAULT_ARG_CHARS = 400  # paths, patterns, queries -- p90 under 130
MCP_ARG_CHARS = 1200     # which page, which filter, which channel: the step

MAX_TAIL_CHARS = 200
MAX_TAIL_LINES = 3

# A pasted stack trace is noise wherever it appears, including inside a prompt.
# Developers paste logs constantly -- in one transcript, four prompts carried
# 28KB of traceback between them, which was 70% of the whole extract. The ask
# usually opens the message ("any hints here?") and occasionally closes it, so
# both ends are kept and the middle goes. The elision is left visible because
# "they pasted a large log here" is itself worth knowing.
MAX_PROMPT_CHARS = 800
PROMPT_HEAD_CHARS = 500
PROMPT_TAIL_CHARS = 200

# What the agent *said*, not just what it did. Some practices worth capturing
# live entirely in the dialogue: proposing a plan and inviting the developer to
# challenge it, or asking for scope and limits before touching code. None of
# that appears in a tool call. An earlier version kept assistant prose only
# when a short reply followed it, which preserved 1.7% of it.
MAX_SAID_CHARS = 600
MAX_PLAN_CHARS = 900

# Slash-command plumbing, which reaches the transcript as ordinary user text.
_PLUMBING = re.compile(
    r"<(local-command-[a-z]+|command-(name|message|args))>.*?</\1>",
    re.S,
)
_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)

# Filler inside a command, measured on this corpus as a share of all Bash
# argument bytes. Truncation is blunt -- it drops the end of a command to make
# room for noise at the front. Removing the noise first is strictly better.
#
# A home-directory prefix is identical in every command of a session and says
# nothing about the procedure (7.8%). An `echo "==="` separator is formatting
# the agent printed for its own reading (7.6%). Output plumbing and grep
# excludes were measured too and left alone: under 0.5% each, and `2>&1` can
# be the reason a step behaved as it did.
_HOME_PREFIX = re.compile(r"/Users/[^/\s\"']+/")
_ECHO_RULE = re.compile(r"""echo\s+["']?[-=*_]{3,}[^;&|"']*["']?\s*;?\s*""")

# A heredoc body is content being written to a file, which is the same thing as
# `Write`'s content and `Edit`'s new_string -- and those are capped at 250 above
# for a reason already measured here: the step is "wrote the file", the content
# is the product. A heredoc is `Write` spelled in shell, so it gets the same
# allowance rather than Bash's 1500.
#
# Measured on a 12 MB session: 23 such bodies held 18,319 characters, 30.6% of
# all Bash text, and were mostly commit messages staged through /tmp. Capping
# them takes 5.0% off the whole extract.
#
# Not dropped outright, because the comment on Bash's own limit is right that
# heredocs carry conventions worth keeping -- and a convention is stated at the
# top of a file, not buried at the end. 250 characters keeps the commit
# subject, the shebang, the first rule; it is the body underneath that repeats
# what the surrounding commands already say.
_HEREDOC = re.compile(r"(<<-?\s*['\"]?(\w+)['\"]?\n)(.*?)(\n\s*\2\b)", re.S)
HEREDOC_CHARS = 250


def _clip_heredocs(command: str) -> str:
    """Shorten what a heredoc writes, keeping the command that writes it.

    Runs before newlines are flattened: once the command is one line the body
    has no delimiter left to find, which is why this was invisible the first
    time the extract was measured for compressible bulk.
    """
    def clip(match: re.Match) -> str:
        opener, _name, body, closer = match.groups()
        if len(body) <= HEREDOC_CHARS:
            return match.group(0)
        return (f"{opener}{body[:HEREDOC_CHARS]}"
                f"\n[…{len(body) - HEREDOC_CHARS:,} chars written…]{closer}")
    return _HEREDOC.sub(clip, command)


def _declutter(command: str) -> str:
    """Strip filler from a shell command without changing what it did."""
    command = _HOME_PREFIX.sub("~/", command)
    command = _ECHO_RULE.sub("", command)
    return command.strip(" ;&")


def _is_command_body(text: str) -> bool:
    """True for a user message that is really an expanded command definition.

    Detected by shape rather than by name: long, opens with a markdown heading,
    and carries several sections. A person does not type that.
    """
    if len(text) < 400:
        return False
    return bool(re.match(r"\s*#\s+\S", text)) and text.count("\n##") >= 2


def _clean(text: str) -> str:
    text = _PLUMBING.sub("", text)
    text = _SYSTEM_REMINDER.sub("", text)
    return text.strip()


def _shorten_prompt(text: str) -> str:
    """Keep both ends of a long prompt and say what was dropped."""
    if len(text) <= MAX_PROMPT_CHARS:
        return text
    dropped = len(text) - PROMPT_HEAD_CHARS - PROMPT_TAIL_CHARS
    return (f"{text[:PROMPT_HEAD_CHARS]}\n"
            f"    […{dropped:,} characters pasted…]\n"
            f"{text[-PROMPT_TAIL_CHARS:]}")


def _shorten(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


def _arguments(name: str, tool_input: dict) -> str:
    if name == "AskUserQuestion":
        return "\n    " + _questions(tool_input)
    if name == "ExitPlanMode":
        plan = str(tool_input.get("plan") or "")
        return "\n    " + _shorten(plan, MAX_PLAN_CHARS)
    keys = ARG_KEYS.get(name)
    if keys is not None:
        values = [str(tool_input[k]) for k in keys if tool_input.get(k)]
    else:
        values = [f"{k}={v}" for k, v in list(tool_input.items())[:2]
                  if isinstance(v, (str, int, float, bool))]
    if name == "Bash":
        values = [_clip_heredocs(v) for v in values]
    argument = " ".join(values).replace("\n", " ")
    argument = _declutter(argument) if name == "Bash" else _HOME_PREFIX.sub("~/", argument)
    limit = ARG_CHARS.get(
        name, MCP_ARG_CHARS if name.startswith("mcp__") else DEFAULT_ARG_CHARS)
    if len(argument) <= limit:
        return argument
    # Both ends: the head carries the command and its flags, the tail carries
    # how it closed -- a truncated heredoc loses the content it was writing.
    head, tail = int(limit * 0.75), limit - int(limit * 0.75)
    return (f"{argument[:head]}"
            f" […{len(argument) - limit:,} chars…] "
            f"{argument[-tail:]}")


def _outcome(text: str, is_error: bool) -> str:
    """The tail of a result: enough to see what happened, not the whole thing."""
    marker = "!" if is_error else "="
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return f"    {marker} (no output)"
    tail = "\n".join(lines[-MAX_TAIL_LINES:])[-MAX_TAIL_CHARS:]
    return f"    {marker} " + tail.replace("\n", "\n      ")


def extract(messages: list[Message]) -> str:
    """Render messages as compressed, readable text.

    Prompts, the tool calls they produced, and the outcomes that mattered.
    """
    lines: list[str] = []
    tool_names: dict[str, str] = {}  # tool_use_id -> tool name

    for message in messages:
        if message.role == "user":
            results = [b for b in message.blocks if b.kind == "tool_result"]
            if results:
                for block in results:
                    name = tool_names.pop(block.tool_use_id, "")
                    if block.is_error or _keeps_outcome(name):
                        lines.append(_outcome(block.text, block.is_error))
                continue

            text = _clean(message.text)
            if not text or _is_command_body(text):
                continue
            lines.append(f"\n> {_shorten_prompt(text)}")
            continue

        for block in message.blocks:
            if block.kind == "tool_use":
                tool_names[block.tool_use_id] = block.name
                lines.append(f"  $ {block.name} {_arguments(block.name, block.tool_input)}".rstrip())
            elif block.kind == "text" and block.text.strip():
                lines.append(f"  . {_shorten(block.text, MAX_SAID_CHARS)}")

    return "\n".join(lines).strip("\n")
