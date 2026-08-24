---
description: Record a procedure the developer is describing rather than one that was observed
argument-hint: "<what the skill should do>"
allowed-tools: Bash(python3 bin/skillpp *), AskUserQuestion
---

# A skill from a description

The developer is telling you about a procedure rather than doing it in front of
you. `$ARGUMENTS` is what they said.

## First, find what the description leaves out

A described procedure is missing things a watched one never is: the person
knows the parts they do without thinking, and those are exactly the parts a
future agent will not. Before writing anything, look for:

- **A trigger.** When should this fire? A body with no "use this when" is a
  skill that never loads.
- **A format referred to but never given.** "The usual report shape", "our
  ticket format" — ask what it actually is.
- **Unconstrained sources.** "Check the logs" — which, where?
- **What failure looks like.** The step that goes wrong, and what to do then.
- **A procedure too thin to be one.** Two obvious commands is a note, not a
  skill. Say so rather than padding it.

Use **AskUserQuestion** for at most three of these, the three that would most
change what gets written. Do not ask what you can infer from the repository —
read it first.

## Then write the body

The body of a SKILL.md: imperative instructions for a future agent, no
frontmatter and no `# Title`. Same shape as `/log-session` produces, and the
same rules — say when *not* to use it, keep a number only when the number is
the rule, redact any credential.

Write what they described, plus what the answers filled in. Do not invent
steps they did not mention to round it out: a described procedure with three
real steps is worth more than one with six, three of which you guessed.

## Record it

```bash
python3 bin/skillpp dictate-skill --name <name> <<'SKILL'
<the body>
SKILL
```

Gerund, lowercase, hyphenated, as everywhere else.

It skips the recurrence threshold — three sightings are a proxy for "worth
someone's attention", and the developer has just supplied that judgement
directly — so it is waiting in `/review-candidates` immediately. Promotion is
still theirs.

Note there is no trace behind it, so `skillpp redraft` (`python3 bin/skillpp redraft`) cannot rewrite it later:
nothing was observed. If the body turns out wrong, the procedure has to be
described again.

## Report

One line: the name recorded, and which gaps you asked about. If you judged it
too thin to be a skill, say that instead and record nothing.
