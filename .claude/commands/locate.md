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

- `landed` — **something was carried out, and then confirmed.** Both halves are
  required. A file written, an issue updated, a service deployed, a message
  sent — followed by a gate passing, a commit, a health check, or the developer
  turning to a different subject.
- `open` — anything else. Changed but unverified, still being investigated, the
  agent's own account says it is unresolved, or nothing was carried out at all.

**A request answered without carrying anything out is `open`, however
completely it was answered.** "Is this cache going to blow up memory?" —
answered accurately from one grep, nothing left hanging — is still `open`,
because there is no procedure here to extract. `open` is doing double duty: it
means both *unresolved* and *nothing to see*. What unites them is the only thing
this verdict is used for — do not look for a procedure here.

Nothing on its own makes a segment `landed`. Not a commit, not a passing gate,
not the developer moving on. Those corroborate a change that was carried out;
without one they mean nothing.

Two verdicts, not three. An earlier version separated "nothing happened here"
from "unresolved", and every disagreement it produced was between those two —
because reading that is the first half of a procedure is both. Downstream they
mean the same thing, which is *do not start a span here*, so the distinction was
costing accuracy and buying nothing.

"Moved on to different work" is the phrase to weigh carefully. A developer who
extends the same change — "now also cap it per tenant" — has not accepted it as
finished, they have asked for more of it. That segment is `open`. Acceptance
looks like a new subject.

That is the whole question. **Do not decide whether anything is worth keeping**,
do not name it, do not judge which of its steps matter, and do not write a
skill. Those are separate questions asked later with more context than you have
here. A segment can be `landed` and completely mundane — most are.

## When part of a request is withheld

The block may say it is showing only some aspects of each request. That is
deliberate, and it is not a reason to refuse or to hedge: answer from what is
shown. Answer `open` when the shown aspects contain nothing, rather than refusing.

If the agent's account is withheld, you are judging from the commands alone. Ask
whether the request's work was *carried out*, not where it sits in the list:

- Did something get changed — a file written, an issue updated, a message
  posted, a release deployed? If nothing was, the answer is `open`.
- Is there anything showing that change failing? If not, treat it as having
  worked. Absence of a failure is the only success signal a command list has.

**Where the last command sits is not evidence.** A segment often ends with
unrelated work — a few greps after the real change, a status check, the start of
the next thing — and reading the final line as the verdict marks finished
procedures unresolved. That is the worst error available here: an `open` verdict
on finished work means nothing looks at it again, while a wrong `landed` only
costs a later judgement that finds nothing.

## Read the account, not just the commands

When it is shown, the last line of a segment is usually the agent saying what
happened, and it is the most reliable evidence in the block. "Green, committed" is `landed`.
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
2 open
```

Every segment in the block gets exactly one line, in order. If you are torn
between `landed` and `open`, choose `open`: a procedure wrongly called finished
becomes a skill for a route nobody walked to the end, and that costs more than
one missed later.
