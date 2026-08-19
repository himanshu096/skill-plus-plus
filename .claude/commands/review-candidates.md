---
description: Go through proposed skills and decide which to make real
argument-hint: "[a candidate name — defaults to the strongest one]"
allowed-tools: Bash(python3 bin/skillpp *), AskUserQuestion
---

# Decide on a proposed skill

## What is waiting

!`python3 bin/skillpp candidates --open-only --json`

If that is empty, say so in one line and stop. Do not go looking for work.

`$ARGUMENTS` names a candidate to look at. With no argument, take the first —
they arrive ordered by how often the procedure has been seen.

## Show it

Read the candidate's `body` and say, in **two or three lines**: what the
procedure does, and how often it has been seen. Nothing else — the body is
right there and the developer can read it.

## Ask

Use **AskUserQuestion**. One question, these options:

- **Make it a skill** — write it to the skills directory now
- **Leave it for now** — stays a candidate; comes up again when it recurs
- **Look at another** — move to the next candidate

One candidate at a time.

## If they say yes

Two fields decide whether the skill is ever found. Both are read at startup,
before the body loads, so a skill with a weak description simply never fires.
Write them rather than letting the description default to the body's first line.

- `--description` — what it does *and* when to use it, third person
- `--when-to-use` — the trigger: phrases someone would actually say, or the
  situation they would be in. Appended to the description in the listing

```bash
python3 bin/skillpp promote-candidate "<name>" \
  --description "<what it does. Use when <the situation>.>" \
  --when-to-use "<trigger phrases or an example request>"
```

Then read back the path it wrote and stop. Do not open the file, tidy it, or
propose the next candidate unprompted.

## If they say no

Say nothing further about it. The candidate stays as it is, and will come up
again when the procedure recurs — that is the intended behaviour, not a thing
to work around.
