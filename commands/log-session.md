---
description: Review this session for anything worth turning into a skill
argument-hint: "[transcript path or session id — defaults to this session]"
allowed-tools: Bash(python3 bin/skillpp *)
---

# Review this session

## The session

!`python3 bin/skillpp prepare-session $ARGUMENTS`

The block above was produced before you were asked anything, so it is the
session as it stands rather than your recollection of it. If it tells you to
stop, stop — say so in one line and write nothing.

Notation: `>` a developer prompt · `$` a tool call · `=` it worked · `!` it
failed · `.` what the agent said.

## What to look for

A **completed procedure worth turning into a skill**. Completed means it
reached its intended end: a final step that succeeded, or the developer
accepting the result and moving on. Discard anything abandoned partway, still
in progress when the session ends, or that failed outright — a half-finished
route is not a recipe.

That is about the procedure as a whole, not its steps. A step that failed
*inside* a procedure that finished is usually the reason the route took the
shape it did, and belongs in the skill as the rule that prevents it recurring.

Most sessions contain nothing. That is the common and correct answer.

## What to write

For each procedure, the body of a SKILL.md. **Do not create any skill file** —
these are proposals for review, and promoting one is a separate deliberate act.

Write **imperative instructions for a future agent to follow**, not a record of
what happened this time. Match specificity to how fragile the step is — an
exact command where a flag or an ordering matters, general guidance where
judgement is fine.

No frontmatter, and no `# Title`: the name is passed separately below, and
repeating it in the body only creates two places for it to disagree. Start
straight at the instructions.

Strip anything belonging to this one run: ids, filenames, exact figures. Keep a
number only when the number *is* the rule — a required flag, a URL suffix, a
threshold. A failure earns a line only as a rule that prevents it recurring,
never as an account of it happening.

**Say when *not* to use it.** A short list of the situations a reader might
mistake for this one, and what to do there instead — an active virtualenv, a
project that already declares its own command, a request to write the thing
rather than run it. A skill that only says when it applies fires on the
neighbouring case too, and a wrong skill is worse than a missing one because
the agent stops reasoning and follows it.

Redact any credential, token or key rather than repeating it.

Name it in the gerund, lowercase and hyphenated — `reconciling-deck-against-article`,
`bootstrapping-an-ephemeral-test-runner`.

## Then record each one

Run the commands below **exactly as written**, with no flags beyond the ones
shown. Paths printed in the block above are there to be read, not passed back:
handing one to `--root` builds a second store inside the first, and every
count starts again from zero with nothing reporting a problem.

**You do not decide whether a proposal matches something already recorded.**
`--auto-match` decides it, by comparing this session against the sessions that
produced each stored procedure. That comparison is arithmetic rather than
judgement, it does not get worse as the store grows, and it is not something to
second-guess from the listing above.

One call per proposal:

```bash
python3 bin/skillpp record-candidate $ARGUMENTS \
  --name <name> --auto-match <<'SKILL'
<the body>
SKILL
```

What the command reports back tells you what it decided: `recorded` for a
procedure it had not seen, `merged into '<name>'` for one it recognised, and a
score in brackets either way. A line saying the score was *too close to call
locally* means it filed the proposal as new and a person will reconcile it — that
is the intended outcome for an ambiguous case, not a failure.

The count does not decide whether an entry belongs; your judgement already did
that. It decides what a person is asked about, and when. A procedure seen once
or twice is recorded and left to accumulate — it does not reach
`/review-candidates` until it has recurred enough to be a pattern, so record
freely and do not weigh whether something is "worth promoting". That is a
different question, asked later, by someone else.

## Finally, mark the session reviewed

```bash
python3 bin/skillpp commit-session $ARGUMENTS
```

Once, after the recording, whether there were three proposals or none — a
session that found nothing still has to record how far it read, or its messages
are reviewed again forever. Run it last: a bookmark moved ahead of a failed
review buries those messages behind a claim that they have already been seen.

## Report

One line: the path written, and the name of each proposed skill. If you judged
something borderline — a procedure that might not have finished, or might not
be general enough — say which call you made and why.

If you recorded anything, add one line pointing at `/review-candidates`, which
is where a candidate is actually decided on. Do not ask about promotion here:
this command reviews a session, and a session that yields three proposals
should not turn into three questions.
