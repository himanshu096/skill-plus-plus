---
description: Rewrite a recorded procedure's body against what the session actually ran
argument-hint: "<candidate name>"
allowed-tools: Bash(python3 bin/skillpp *)
---

# Redraft a body

## What is stored now

!`python3 bin/skillpp redraft $ARGUMENTS --show`

The block above holds the body as it stands and the trace of what the sessions
that taught it actually ran. The body generalises; the trace does not.

## Why a body gets redrafted

Not because it aged. Because following it did not work — a step it never
mentioned turned out to be required, or one it insists on turned out to be
wrong. The trace is here so that can be checked against what was really done,
rather than guessed at.

Read both. Then write the body again, keeping every rule that still holds and
adding the ones the trace shows were needed. Do not restate the trace: it is
evidence, not the skill. A body that lists commands in order is a transcript,
not instructions.

If the current body is already right, **say so and write nothing**. A redraft
that changes wording without changing meaning costs a review and teaches
nobody anything.

## Hand it back

Print the new body between these two markers, on their own lines, and write no
files — the command writes it where it belongs:

```
<<<SKILLPP-BODY
...the rewritten body, no frontmatter, no `# Title`...
SKILLPP-BODY>>>
```

Do not touch `~/.claude/skills`, and do not promote. A skill's text changing
under the developer without review is the failure this command exists to
avoid.

If the body is already right, print **no markers at all** and say why in one
line.

## Report

After the markers, one line: what rule the redraft adds that the old body did
not have.
