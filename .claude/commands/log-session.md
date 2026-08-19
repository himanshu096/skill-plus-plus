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

Redact any credential, token or key rather than repeating it.

Name it in the gerund, lowercase and hyphenated — `reconciling-deck-against-article`,
`bootstrapping-an-ephemeral-test-runner`.

## Then record each one

The store shown above holds every candidate from every session ever reviewed —
not just this conversation's. Your only decision per proposal is whether it
describes **a procedure already in there**, judged on what the procedure *does*
rather than what it is called, because the same work gets named differently
every time.

**A match.** The body you send **replaces** the one already stored, so write the
two combined rather than only today's. The existing body is printed above —
take what it knows, add what this session added, drop what turned out wrong.
Sending only today's account silently discards everything the earlier session
had learned.

```bash
python3 bin/skillpp record-candidate $ARGUMENTS \
  --name <clearer-of-the-two-names> --matches "<the existing name>" <<'SKILL'
<the two bodies merged>
SKILL
```

**Something new:**

```bash
python3 bin/skillpp record-candidate $ARGUMENTS \
  --name <name> <<'SKILL'
<the body>
SKILL
```

One call per proposal. Everything that follows — the count, the provenance, the
`also seen as:` line when a name changes, the ordering by count, rewriting the
file — happens in the command. You never edit the document or restate anything
already in it, so no existing candidate can be lost by being overlooked.

Ordering is by count, highest first. The count does not decide whether an entry
belongs; your judgement already did that. It decides what a person reads first.

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
