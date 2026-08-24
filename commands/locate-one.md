---
description: Say whether the work in one request reached a resolved state
argument-hint: "[transcript path] --only N [--aspect ...]"
allowed-tools: Bash(python3 bin/skillpp *)
---

# Did this one request's work land?

> **Not part of the review loop.** `/log-session` and `/review-candidates` are
> the daily path and neither calls this. This measures whether a *local* model
> can say where work resolved, which is the cheap half of detection described
> in `docs/windowing.md` — a component that is built and measured but not yet
> wired into the pipeline. Ten `locate-*` eval cases keep it honest.

## The request

!`python3 bin/skillpp segments $ARGUMENTS`

Notation: `>` a developer prompt · `$` a tool call · `=` it worked · `!` it
failed · `.` what the agent said.

If the block above tells you to stop, stop — say so in one line and nothing else.

## The question

**Did the work in this request reach a resolved state?**

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

## What you are not being asked

Do not decide whether this is worth keeping, do not name it, do not judge which
of its steps matter, and do not write a skill. Those are separate questions
asked later with more context than you have here. Most requests are `landed` and
completely mundane.

You are seeing one request out of several, and beneath it what the developer
said next. **That line is context, never the thing you are judging.** Use it for
one purpose: deciding whether they moved on.

- A **new subject** means they accepted what happened and left it. That
  corroborates `landed` — but only if something was actually carried out in the
  request above. A new subject after a request that changed nothing is still
  `open`.
- **More of the same** — refining it, retrying it, asking why it did not work —
  means it had not resolved. That is `open` however much happened.
- **Nothing, because this was the last request**, is weak evidence either way. A
  session can end on finished work or be abandoned mid-task. Decide on the
  request itself: something carried out, and nothing showing it failed.

## When part of the request is withheld

The block may show only some aspects of the request. That is deliberate, and not
a reason to refuse or to hedge — answer from what is shown. If the agent's
account is withheld you are judging from the commands: ask whether something was
carried out and whether anything shows it failing, and treat the absence of a
failure as the only success signal a command list has.

Where the last command sits is not evidence. A request often ends with
unrelated work — a status check, a few greps, the start of the next thing.

## Answer

**One word, and nothing else.** No number, no punctuation, no explanation:

    landed

or

    open

If you are torn, answer `open`. A request wrongly called resolved becomes a
skill for a route nobody walked to the end; one wrongly called open costs a
later look that finds nothing.
