# Skill Plus Plus

**Your working knowledge, captured as you work, kept only when you say so.**

---

## The problem

Everyone has a handful of procedures they carry in their head. How this project's
release actually goes. What to check first when the nightly job fails. The three
things you always do before opening a pull request.

None of it is written down. Writing it down is a separate job that competes with
the work itself, so it loses — and when it does get written, it goes stale
quietly, because nothing tells you the script it mentions was renamed six months
ago.

The cost is invisible but constant. New people take a month to learn what a
colleague could have told them in ten minutes. Everyone rediscovers the same
workaround. And when someone leaves, the procedure leaves with them.

Skill Plus Plus exists because the moment you *have* the knowledge and the moment
you're willing to *write it down* almost never coincide.

---

## What it is

A quiet observer of how you actually work, and a deliberate gate between what it
noticed and what becomes real.

It watches your working sessions and keeps short summaries of what you did. When
the same piece of work happens often enough — or when you simply say "keep that"
— it offers to turn it into a reusable procedure. You review it, answer at most
three questions about the parts it couldn't infer, and approve. Only then does
anything get written.

The result is a plain document your AI assistant can follow, and any colleague
can read.

---

## How it works

**It notices.** As you work, it keeps a compact record of what was done —
compressed and stripped of anything sensitive before it is ever stored. It has a
sense of when a task ends: tests passing, a commit landing, a deploy going out.
It has learned to ignore the looking-around that precedes real work, and to
discard sessions where nothing actually got done.

**It waits.** Nothing interrupts you. There are no popups and no suggestions
mid-task. Everything it notices sits quietly until you go looking.

**You review.** When you ask, it shows you what it found — leading with what a
procedure would actually *do*, not what it's for. A summary that sounds right can
sit on top of steps that are wrong, so you see the real actions first, alongside
the sessions they came from.

**It asks only what it can't know.** Not a questionnaire — the gaps themselves
generate the questions. If a command failed and then succeeded with an extra
flag, it asks what told you to reach for that. If the same step ran differently
on different days, it asks whether that's a setting or a correction. If you
described a format but never gave one, it asks for an example. A clean submission
gets no questions at all.

**You approve.** Nothing is written without you. Anything left unanswered is
recorded as an open gap rather than guessed at.

---

## The two ways in

**Say so.** You've just done something worth keeping — say "keep that", or
describe a procedure in plain English. This works today and works well. It skips
any waiting: an explicit request is not noise.

**Let it notice.** Work normally, and let repetition speak. Something done three
times is more likely to be a real procedure than something done once. This is the
more ambitious half, and the one still being proven.

Both end in the same place: a document you reviewed and approved.

---

## What happens afterwards

Procedures are not write-once. They rot, and the rot is silent.

**It notices decay.** Not by counting how long since you used something — the
runbook you need twice a year is precisely the one a timer would delete. It
checks whether the things a procedure *refers to* still exist. A renamed script
or a removed option is real decay; going unused for six months is not.

**It keeps quiet things quiet.** Rarely used procedures stop cluttering the
everyday list without being deleted. Nothing you approved is ever thrown away.

**Ignoring is reversible.** Say you don't want something and it goes to a visible
list you can browse and undo — a parking space, not a shredder. Deleting a
procedure parks it there too, rather than nagging you to recreate it.

**But it keeps count.** If you park something and then keep doing that work by
hand, it says so. Not by re-proposing it — by noting, once, that the evidence has
changed. An ignore means "not now", and sometimes "now" arrives.

**There is a page for all of it.** A local browser view lists what you have, what
is waiting, and what you parked — readable, filterable, editable, with archive
and delete behind a confirmation. It runs on your own machine and is reachable
from nowhere else. Some people would rather see a library than list it.

---

## What makes it different

**Nothing is written without you.** Every procedure was read and approved by a
person. That is the whole design, not a safety feature bolted on.

**Review is meant to take seconds.** Leading with real actions, showing the
sessions something came from, and asking at most three pre-filled questions — all
of it exists so that a fast review is still a real one. If approving everything
becomes reflexive, the gate has failed.

**It stays on your machine.** The record of your work never leaves. A finished
procedure leaves only when you choose to share it — and it is scanned for
credentials and personal data before it can.

**It knows what it doesn't know.** Gaps are recorded as gaps. A procedure that
says "we never established what happens when this fails" is more useful than one
that quietly invented an answer.

---

## Where it works

Best in the terminal, where it can both observe your work and use the procedures
it creates.

In the desktop and web apps, procedures work once shared to your account — but
nothing is observed there. If most of your work happens in chat, the "say so"
route is the one that will serve you.

---

## What it deliberately doesn't do

- **It doesn't write on its own.** No approval, no procedure.
- **It doesn't interrupt.** Everything waits for you to ask.
- **It doesn't delete your work.** Things are demoted, parked, or archived — never
  removed.
- **It doesn't send anything anywhere.** Sharing is always a deliberate act.
- **It doesn't pretend to be sure.** Open questions are labelled, not answered.

---

## Honest status

**Working today:** capturing a procedure on request, describing one in plain
English and having the gaps found, reviewing and approving, and the whole
after-life — decay detection, quiet demotion, the reversible ignore list.

**Still being proven:** whether passive observation, left alone for a week,
surfaces something genuinely worth keeping. Early attempts produced sessions, not
procedures — whole afternoons rather than the four steps that mattered. That has
been substantially reworked, but reworked is not the same as demonstrated.

The question the next fortnight answers is a single one: **did anything it noticed
on its own become something you kept?** If the answer is no, the honest product is
smaller — the parts above that already work — and worth having anyway.

**Not built yet:** sharing across a team, which is where this becomes most
valuable and where the harder questions live. What travels with a procedure and
what stays private. Whether something one person has run twice is ready for
everyone else.
