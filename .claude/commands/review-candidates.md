---
description: Go through proposed skills and decide which to make real
argument-hint: "[a candidate name — defaults to asking]"
allowed-tools: Bash(python3 bin/skillpp *), AskUserQuestion
---

# Decide on proposed skills

## What is waiting

!`python3 bin/skillpp candidates --open-only --json`

## The whole store, for context

!`python3 bin/skillpp candidates`

The first block is the review queue: only procedures seen enough times to be
worth a decision. The second is everything recorded, including the ones still
accumulating sightings.

If the queue is empty, say so in **one line**, name how many are still logging
and how many sightings the nearest one needs, and stop. Do not offer to promote
something below the threshold, and do not go looking for other work — a
procedure seen once is a thing that happened, not a pattern.

## List them all

Every candidate in the queue, one line each: name, how often it has been seen,
and what the procedure does in a short clause.

List **all of them before asking anything**. A developer deciding on one wants
to know what else is waiting — showing only the strongest turns a queue into a
sequence of surprises, and the ones behind it never get read.

If `$ARGUMENTS` names a candidate, still list them, then go straight to that
one.

## Ask which one

Use **AskUserQuestion**. One option per candidate — name plus its count — and a
final **Leave them all for now**. With more than three waiting, offer the three
most-seen and say in the question how many others there are.

## Draft it

For the chosen candidate, show:

- what the procedure does, in two or three lines
- the two fields you propose writing, as a draft the developer can react to

Both fields are read at startup, before the body loads, so a skill with a weak
description simply never fires. Neither may be left to default.

- `--description` — what it does *and* when to use it, third person
- `--when-to-use` — the trigger: phrases someone would actually say, or the
  situation they would be in. Appended to the description in the listing

Show the drafted text itself, not a description of it. This is the part a
person can usefully correct, and they cannot correct what they cannot see.

## Ask to accept

**AskUserQuestion** again, on the draft:

- **Write the skill** — promote it with the drafted fields
- **Change the wording** — take their edit, show it again, ask again
- **Leave it for now** — stays a candidate; comes up again when it recurs

## On acceptance

```bash
python3 bin/skillpp promote-candidate "<name>" \
  --description "<what it does. Use when <the situation>.>" \
  --when-to-use "<trigger phrases or an example request>"
```

Read back the path it wrote, then stop. Do not open the file, tidy it, or move
to the next candidate unprompted — deciding on one is the whole job, and a
queue that promotes itself is not a queue anyone controls.

## On refusal

Say nothing further about it. It stays as it is and comes up again when the
procedure recurs — intended behaviour, not a thing to work around.
