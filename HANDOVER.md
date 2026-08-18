# Handover — session review (`feat/pattern-detection`)

Branch: `feat/pattern-detection`. **157 tests pass:** `python3 -m unittest discover -s tests -q`.
No `pyproject.toml` and no `pytest` installed; use `uv run --with pytest pytest tests/`
if you want pytest specifically. Invoke the CLI as `python3 bin/skillpp`.

Numbers marked *measured* come from the transcripts on this machine: ~117 files,
~324 MB, six projects, about 60 days. Re-run them elsewhere before trusting them.

**Level 1 is built and tested end to end.** What follows is what it does, what
it cost to get right, and what is deliberately not done.

---

## What this does

`/log-session` reviews a session and proposes skills.

```
transcript ──▶ prepare-session ──▶ the model judges ──▶ record-candidate ──▶ commit-session
               (deterministic)      (the only              (deterministic)     (watermark)
                                     judgement)
```

Output is a **SKILL.md body**: imperative instructions a future agent can follow,
not an account of what happened. That distinction took most of the session to
arrive at and is the thing most easily lost — see *How the goal moved*.

Storage, all under `~/.claude/skillpp/`, never in a repo:

| path | what |
| --- | --- |
| `conversations/<conversation-id>.md` | the store: current best description of each candidate, counted and ordered |
| `reviews/<session-id>.md` | the deliveries: what each session proposed, in its own words |
| `sessions/<session-id>.json` | unrelated — hook capture state, written by `capture.py` |

Only a *promoted* skill lands in a repo, at `.claude/skills/<name>/SKILL.md`.
Memories are personal; skills are the team artifact.

---

## Division of labour (agreed, don't cross it)

**Their layer — categorization and lifecycle.** `ledger.py` / `cli.py` own
`candidate` → `promoted` → `ignored`, occurrence counting, `find_match`,
`reconcile`. Treat as status quo.

**Our layer — review.** Deciding what in a session is worth proposing.

⚠️ **Live conflict.** Their layer gates on recurrence reaching a threshold. Ours
does not: judgement decides whether a candidate is recorded at all, and the
count only *orders* what is already in. Nothing bridges the two yet. Raise it
before either side builds further.

---

## How the goal moved, and why

Three reversals, each forced by something that did not work. Knowing them stops
the old shapes being reintroduced.

**Counting recurring workflows → judging skill-worthiness.** A count ranks the
wrong things: "run the test suite" recurs twenty times and deserves no skill,
"file a ticket the team's way" recurs four times and clearly does. What makes
the second valuable is that it encodes a convention, not that it repeats.

**Then: judging → extracting a SKILL.md.** The first working prompt produced a
retrospective report — *Intent / Steps / Worked / Did not work*. Real findings,
wrong shape. A skill is forward-facing instruction; that was documentation of an
event. Anthropic's own guidance is explicit that a SKILL.md is imperative and
that specificity should match a step's fragility.

**And: the count came back, as ordering.** Gate on judgement, order by count.
The gate decides whether an entry belongs; the count decides what a person reads
first. That kills the original inversion — "run the test suite" never enters, so
it cannot outrank anything.

**What that leaves out, accepted:** slow-burn patterns that only look
significant in aggregate. Something mildly annoying twenty times, which no single
session flags as skill-worthy, never surfaces.

---

## The modules, and what each cost to get right

`skillpp/` is flat; these sit alongside `ledger.py` and the rest.

| module | job |
| --- | --- |
| `transcript.py` | JSONL → `Message`/`Block`. The only module touching undocumented internals |
| `identity.py` | messages → conversation id |
| `extract.py` | messages → compressed text |
| `memory.py` | the memory document: parse, upsert, render |
| `prepare.py` | resolve, slice against the watermark, floor, record, commit |

### Decisions measurement forced

**Conversation id is the first message's uuid, in full.** Not a hash of several:
measured, a prefix of 3+ splits a real conversation whose assistant turn was
regenerated at message index 2, producing two memory files with counts split
across both. One message is the shortest possible prefix and the only one an
early divergence cannot break. Not hashed either — a greppable id is worth more
than an opaque one.

**Turn regeneration is routine.** Interrupt a tool call and the turn re-runs, so
a transcript is **not append-only**. Hence the watermark stores `last_message`
*and* `last_timestamp`: the marked message can cease to exist. Look up by uuid,
fall back to timestamp, and **fail loudly if neither resolves** — the natural
guess ("treat it all as new") silently re-folds a conversation and doubles
everything in it.

**Tool results are kept when they cannot be recovered later.** Not "did it change
something": a local `Read` returns a file still in the repo, while a Confluence
page or tracker query returns state from outside it. Getting this wrong was
measured — with MCP results dropped, a ticket-filing session extracted with the
entire procedure missing.

**Arguments are kept when the argument *is* the step.** `Bash` and MCP calls get
room (1500 / 1200 chars); `Edit` and `Write` get a path and little else, because
their argument is the *product*. Keeping the product cost 4 MB corpus-wide and
pushed 13 more sessions over budget while answering none of intent, steps or
outcome.

**The dialogue is kept, not just the actions.** An earlier version preserved
assistant prose only before a short reply — 1.7% of it. Two of the three target
skill shapes (grounded planning, execution checking) live *entirely* in that
prose and were undetectable in principle. `AskUserQuestion` and `ExitPlanMode`
render their content in full for the same reason.

**Long prompts are trimmed at both ends.** Developers paste stack traces; in one
transcript four prompts carried 28 KB of traceback, 70% of that extract. The ask
sits at one end or the other, so both are kept and the elision left visible.

Extraction now: **324 MB → 12.3 MB, 26×.**

### The bug worth remembering

`record()` used `prepared.transcript.stem[:8]` — truncating a session id for a
tidier provenance line. Real uuids diverge within eight characters, so it never
showed up in 157 unit tests or on real sessions. Two fixtures named
`recurrence-a` and `recurrence-b` collided instantly: the second was read as
already present, so the count **stopped rising with no error**.

Precisely the failure this design exists to prevent, sitting in the code that
prevents it. Now the full stem, with a regression test verified to fail against
the old line.

---

## Where judgement stops and code starts

The rule throughout: **code for anything with one right answer that fails
silently; the model for anything needing judgement.**

The model decides three things and nothing else:

1. is there a completed procedure worth a skill
2. is this proposal new, or the same procedure as one already recorded
3. what the instructions say

Everything downstream — count, provenance, `also seen as:` on a rename, ordering,
atomic rewriting — happens in `memory.py`. The model hands over **one candidate
at a time** and never rewrites the document, so an existing entry cannot be lost
by being forgotten. An earlier design had it rewrite the whole document and
needed a guard against dropped entries; the guard is gone because the failure is
now structurally impossible.

`record` and `commit` are separate: a session yields three candidates or none,
and the watermark moves exactly once either way — **after** the recording, never
before. A mark advanced ahead of a failed review buries those messages behind a
claim that they were already read.

---

## Tested

Unit: 157 tests, stdlib `unittest`.

End to end in the CLI, on real and controlled sessions:

| | result |
| --- | --- |
| extract candidates from a real session | imperative, instance-noise stripped |
| memory + review documents written | frontmatter, provenance, both files |
| idempotence — same session twice | "nothing new", nothing written |
| accumulation across sessions | two candidates in one memory |
| correct *non*-match | declined with a reason: setup vs iteration |
| a session with nothing generalisable | zero proposals, watermark still advanced |
| count merge | `seen 2×` with both sessions listed |
| body merge | every rule from both occurrences present |

`tests/fixtures/seed_recurrence.py` builds two snapshots of one conversation
doing the same procedure twice, the second hitting an obstacle the first did
not. It tests what unit tests cannot — whether the model *reaches for*
`--matches`, and whether a merge combines bodies rather than replacing. Its
`.jsonl` output is gitignored; regenerate it.

**Four bugs were found by CLI testing that 157 unit tests did not catch:**
`${CLAUDE_PROJECT_DIR}` not substituted in a command file, an `allowed-tools`
glob missing a space, a wrong absolute path globbed as a session id, and the
truncated session id above. All silent. Worth remembering before trusting a
green suite as proof a wiring change works.

---

## Open

**The division-of-labour conflict** above. Blocking anything at level 2.

**Merging is unproven on messy input.** Three real runs across related work
produced no match — correctly, the procedures differed — so every observed merge
has been on controlled fixtures. If a merge ever does lose something,
`reviews/<session>.md` still holds what each session originally proposed.

**Level 2 does not exist:** the roll-up across conversations, `PATTERNS.md`, and
promote/reject. Level 1 ends at one correct document per conversation.

**Automatic triggering is deferred, not rejected.** The `SessionEnd` hook still
enqueues to `ended-sessions.jsonl`; nothing drains it. The queue it accumulates
is the evidence for whether manual triggering is enough. `claude -p` works, with
a ~$0.16 context floor per invocation, and **`--no-session-persistence` is
mandatory** or a drain creates a session, which fires the hook, which enqueues,
which the next drain processes.

**Unattended mode is a prompt problem, not plumbing.** Every escape hatch in
`/log-session` currently addresses a human.

---

## Reference — Claude Code facts verified here

- **`` !`command` `` in a command file runs *before* the content reaches the
  model**, and its output replaces the placeholder. No exit-code branching, so
  "nothing to do, stop" must be in the output text.
- **`${CLAUDE_PROJECT_DIR}` is not substituted in a `.claude/commands/` file** —
  the permission checker sees a live shell expansion and refuses. Use a relative
  path; the command runs from the project root.
- **`allowed-tools` globs are literal about spaces.** `Bash(python3 * skillpp *)`
  does not match `python3 bin/skillpp …`.
- **The transcript JSONL structure is documented nowhere.** Only
  `transcript_path` is specified. Hence `transcript.py` as a blast-radius
  boundary: it raises rather than returning `[]` when a file has records but no
  recognisable messages, which is the signature of the format having moved.
- **Transcripts are written asynchronously and may lag** — documented. Harmless
  with a watermark, except on a conversation's final run.
- **Custom commands are now skills**; `.claude/commands/*.md` still works.

---

## Dead ends — do not retry

- **`agent`-type hooks on `SessionEnd`.** Structurally impossible: `KP`'s
  signature accepts no `toolUseContext` while the agent branch throws without
  one, and it fails silently. `command` hooks work fine.
- **Code-based segmentation and fingerprint matching** (`segment.py`,
  `recurrence.py`, parked on `segmentation-fixture`). Against 79 transcripts:
  completion markers ended 10% of episodes, and the matcher rated
  `edit:.md | bash:cd` a **178-occurrence workflow**.
- **`similarity()` on prose.** It splits on ` | ` and scores **0.00 on every
  pair** of prose names, including obvious matches. Structurally inapplicable,
  not merely inaccurate. Nothing calls it now.
- **Lineage key + "last analysed UUID" cursor.** Both halves fail: the key groups
  divergent branches, the pointer breaks when a resume rewrites its tail.
- **Per-pattern files + INDEX.md + a name→id mapping for level 2.** Designed in
  detail, then scrapped. Level 2 should be one document merged the way level 1 is.
- **Skipping the extractor.** The median *increment* — one session end, not a
  whole transcript — is **443 KB raw**, and 72% of increments exceed a ~30k-token
  budget. Without extraction the model reads a prefix and summarises it without
  saying so.

---

## Prompt-design lessons already paid for

- **"Dead ends omitted" was wrong** (`4099010`). A task's failures *were* the
  reasoning. Same distinction the extractor's outcome tails preserve.
- **A sharpening clause can become a loophole.** *"Would they only discover it by
  failing?"* was added to make "not derivable" concrete, and let general shell
  trivia through instead. When adding a test to a prompt, check what it now
  admits, not only what it now excludes.
- **A model will report a tool call it did not make.** Asked to write a file with
  `Write` unavailable, it printed "Wrote SKILL.md — needs your approval" and
  wrote nothing. Constrain with `--allowed-tools ""` rather than trusting the
  narration.
