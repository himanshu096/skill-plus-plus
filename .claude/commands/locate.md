---
description: Say, for each request in a session, whether its work reached a resolved state
argument-hint: "[transcript path or session id — defaults to this session]"
allowed-tools: Bash(python3 bin/skillpp *)
---

# Where did the work land?

## The session, one request at a time

!`python3 bin/skillpp segments $ARGUMENTS`

Notation: `>` a developer prompt · `$` a tool call · `=` it worked · `!` it
failed · `.` what the agent said.

If the block above tells you to stop, stop — say so in one line and write
nothing.

## The one question

For **each numbered segment**, decide only this: **did the work in it reach a
resolved state?**

- `landed` — the work finished. Something was changed and then confirmed: a
  gate passed, a change committed, a deployment reported healthy, or the
  developer accepted it and moved on.
- `open` — work happened and was left hanging. Changed but unverified, still
  being investigated, or the agent's own account says it is unresolved.
- `none` — nothing happened here. A bare acknowledgement, a question answered
  in prose, reading without changing anything.

That is the whole question. **Do not decide whether anything is worth keeping**,
do not name it, do not judge which of its steps matter, and do not write a
skill. Those are separate questions asked later with more context than you have
here. A segment can be `landed` and completely mundane — most are.

## Read the account, not just the commands

The last line of a segment is usually the agent saying what happened, and it is
the most reliable evidence in the block. "Green, committed" is `landed`.
"Nothing conclusive yet — I'd need a profile to say which" is `open`, however
many commands ran above it.

Judge the commands by what they were *for*, not by their names. A test command
is a gate when it confirms a change and orientation when it is checking where
things stand. A commit near the top of a long segment is usually not where the
work ended.

A step that failed and was then retried successfully is `landed` — the failure
is why the route took its shape, not evidence it was abandoned. A step that
failed and was not retried is `open`.

## Answer

One line per segment, nothing else — no preamble, no explanation, no summary:

```
0 landed
1 open
2 none
```

Every segment in the block gets exactly one line, in order. If you are torn
between `landed` and `open`, choose `open`: a procedure wrongly called finished
becomes a skill for a route nobody walked to the end, and that costs more than
one missed later.
