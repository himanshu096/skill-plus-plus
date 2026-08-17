---
description: Read a session transcript and write a per-session task summary
argument-hint: "[transcript path or session id — defaults to the current session]"
---

# Skill Plus Plus — log a session

Read the current session's transcript from disk and write one summary file
describing the tasks that actually happened.

Read the transcript **from the file**, not from your own context. You are running
inside the session so you could summarise from memory, but that path does not
survive automation — the same logic has to work later when a hook triggers it
with no conversation in context. If the file cannot be found, stop and say so
rather than falling back to context: a silent fallback would hide exactly the
failure this command exists to detect.

## 1. Resolve the transcript

**No argument** — summarise the current session:

```bash
SLUG=$(pwd | sed 's|[/._]|-|g')
TRANSCRIPT="$HOME/.claude/projects/$SLUG/$CLAUDE_CODE_SESSION_ID.jsonl"
ls -la "$TRANSCRIPT"
```

The slug replaces `/`, `.` and `_` with `-` — all three, not just the slashes.
`CLAUDE_CODE_SESSION_ID` is set in the shell environment.

**With an argument** — summarise a different session. Accepts either a full path
to a `.jsonl`, or a bare session id resolved against the current project:

```bash
ARG="<the argument>"
if [ -f "$ARG" ]; then
  TRANSCRIPT="$ARG"
else
  SLUG=$(pwd | sed 's|[/._]|-|g')
  TRANSCRIPT="$HOME/.claude/projects/$SLUG/$ARG.jsonl"
fi
ls -la "$TRANSCRIPT"
```

This exists so past sessions can be backfilled, and it is the same path the
eventual automated pass takes — draining a queue means analysing a transcript
that is not the current session.

Either way: derive the session id from the transcript filename stem, not from
the environment, so an argument-driven run records the session it actually
summarised.

If the file does not exist, report the path you tried and stop.

If the transcript is very large, say so with its size before reading, and if it
will not fit, stop and report that rather than reading a truncated prefix — a
summary built from part of a session is worse than none, because nothing marks
it as incomplete.

## 2. Read it

JSONL, one record per line. What matters:

- **user messages** — what was asked
- assistant **`tool_use`** blocks — what actually ran, with arguments

Ignore thinking blocks, tool results, system reminders, and slash-command
plumbing (`<local-command-caveat>`, `<command-name>`, image placeholders).
Those are noise and there is a lot of them.

## 3. Find the tasks

A task is a goal the developer wanted done, plus the work that achieved it. Not
a single turn, and not the whole session. Two calibrations, both from measured
failures — a fingerprint-based approach merged everything, a
prompt-boundary-based approach shredded everything:

- **Do not merge unrelated work.** A session that deployed something *and* fixed
  an unrelated bug contains two tasks, not one.
- **Do not fragment one task.** Follow-ups like *"continue"*, *"next"*,
  *"go through the next three"*, *"fix that"*, *"try again"* belong to the task
  already in progress. A new user message is not a new task.
- **Questions and discussion are zero tasks.** A session spent reading code and
  talking about design produced no workflow, however long it was.

## 4. Nothing substantive?

Write no file. Say so plainly and stop. An empty summary is worse than none —
it implies there was something to capture.

## 5. Write the summary

One file: `.claude/skillpp/memory/sessions/<YYYY-MM-DD>-<session_id>.md`, with
the date taken from the transcript's first record timestamp.

If that file already exists, report it and **ask before overwriting.** Summaries
are meant to be written once; the escape hatch exists only for iterating on
quality during testing.

Format — frontmatter, then one `##` section per task:

```markdown
---
session: <session_id>
date: <YYYY-MM-DD>
---

## <short task title, as the developer would name it>

<one or two sentences: what the goal was, in their framing rather than yours>

Shape:
1. <the steps that mattered, in order. Omit noise — typos, wrong paths, a
   command re-run because output scrolled. But keep a failed attempt when it
   ruled something out and led to the approach that worked: that is the
   reasoning, not clutter.>

Judgement: <the part a command log cannot show — why a particular flag, what the
trap was, why a retry was needed, what you would warn the next person about>
Parameter: <what would differ on another run — a target, a filename, an
environment>
```

On the two fields that carry the value:

- **`Judgement`** is the whole reason this beats mechanical step-matching, and
  the easiest thing to fake by rewording the steps. If the session genuinely
  surfaced no insight, write `none surfaced` — that is an honest answer and more
  useful than padding.
- **`Parameter`** is what makes a workflow reusable rather than a one-off. If
  nothing varies, write `none`.

Describe only what happened; invent nothing. **Redact any credential, token or
key** you encounter rather than copying it into the summary. Keep each task under
roughly 400 words.

## 6. Report

State the path written and the task titles, one line each. If you judged
something borderline — a stretch of work that might be one task or two — say
which call you made and why, so it can be corrected.
