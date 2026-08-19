# Handover — session review (`feat/pattern-detection`)

Branch: `feat/pattern-detection`, one commit ahead at `9e8e907` plus uncommitted
work described below. **168 tests pass:** `python3 -m unittest discover -s tests -q`.
No `pyproject.toml`; use `uv run --with pytest pytest tests/` if you want pytest.
Invoke the CLI as `python3 bin/skillpp`.

Numbers marked *measured* come from the transcripts on this machine: ~117 files,
~324 MB, six projects, about 60 days.

**Detection is built and live-tested end to end.** The current task is testing
it further — what to try, and what already passed, is under *Testing* below.

---

## What this does

```
/log-session          review a session, propose skills          ─┐
                                                                 │  PATTERNS.md
/review-candidates    decide on one, write it out as a skill    ─┘
```

Two documents, and only two:

| path | what |
| --- | --- |
| `~/.claude/skillpp/PATTERNS.md` | the store — every candidate, counted, ordered, in two sections |
| `~/.claude/skillpp/reviews/<session-id>.md` | what a session proposed **and how far it read** |
| `<repo>/.claude/skills/<name>/SKILL.md` | a promoted skill — the only thing that lands in a repo |

Memories are personal. Skills are the team artifact.

### The flow

```
transcript ─▶ prepare-session ─▶ the model judges ─▶ record-candidate ─▶ commit-session
              (deterministic)     (the only            (deterministic)     (bookmark)
                                   judgement)
                                        │
                                        ▼
                              /review-candidates ─▶ promote-candidate
                              (asks the developer)   (writes the SKILL.md)
```

The model decides **three things and nothing else**: is there a completed
procedure worth a skill, is it new or the same as one already recorded, and what
the instructions say. Counting, provenance, dates, ordering, status, atomic
writes — all code.

It hands over **one candidate at a time** and never rewrites the store, so an
existing entry cannot be lost by being forgotten. An earlier design had it
rewrite the whole document and needed a guard against dropped entries; the guard
is gone because the failure is now impossible.

---

## Uncommitted since `9e8e907`

The commit describes a per-conversation store. **That was replaced.** Working
tree holds:

- **`PATTERNS.md` replaces `conversations/<id>.md`.** A per-conversation store
  meant a count could only rise when one conversation repeated a whole procedure
  — and 82% of conversations are a single session, so counts sat at 1 and the
  ordering they were meant to drive did nothing. One store makes recurrence
  visible.
- **The bookmark moved into `reviews/`.** Each review records `last_message` and
  `last_timestamp`, so there is no third place for state to live and disagree.
  A session that proposes nothing still writes one, or its messages are read
  again forever.
- **Status:** `candidate → promoted`, with `skill_path` and dates.
- **New commands:** `candidates`, `promote-candidate`, and `/review-candidates`.

---

## Where judgement stops and code starts

The rule throughout: **code for anything with one right answer that fails
silently; the model for anything needing judgement.**

Two axes are kept deliberately separate in `memory.py`:

- **status** decides which section an entry sits in, and only ever changes
  because a person decided something
- **evidence** (the count) orders entries within a section and promotes nothing
  on its own

Conflating them is what made an earlier design rank "run the test suite" above
"file a ticket the team's way". The count is derived from the provenance list
rather than stored beside it, so the two cannot drift.

`record` and `commit` are separate: a session yields three candidates or none,
and the bookmark moves exactly once either way — **after** the recording, never
before. A mark advanced ahead of a failed review buries those messages behind a
claim that they were already read.

---

## The modules

`skillpp/` is flat; these sit alongside `ledger.py` and the rest.

| module | job |
| --- | --- |
| `transcript.py` | JSONL → `Message`/`Block`. The only module touching undocumented internals |
| `identity.py` | messages → conversation id |
| `extract.py` | messages → compressed text (**324 MB → 12.3 MB, 26×**) |
| `memory.py` | the store: parse, upsert, promote, render |
| `prepare.py` | resolve, slice against the bookmark, floor, record, commit |

### Decisions measurement forced

**Conversation id is the first message's uuid, in full.** A prefix of 3+ splits
a real conversation whose assistant turn was regenerated at message index 2. One
message is the shortest possible prefix and the only one an early divergence
cannot break. Not hashed — a greppable id is worth more than an opaque one.

**Turn regeneration is routine**, so a transcript is **not append-only**. The
bookmark stores `last_message` *and* `last_timestamp`: the marked message can
cease to exist. Look up by uuid, fall back to timestamp, and **fail loudly if
neither resolves** — the natural guess ("treat it all as new") silently re-folds
a conversation and doubles everything in it.

**Tool results are kept when they cannot be recovered later.** Not "did it
change something": a local `Read` returns a file still in the repo, a Confluence
page returns state from outside it. Getting this wrong was measured — with MCP
results dropped, a ticket-filing session extracted with the whole procedure
missing.

**Arguments are kept when the argument *is* the step.** `Bash` and MCP get room;
`Edit`/`Write` get a path, because their argument is the *product*.

**The dialogue is kept, not just the actions.** An earlier version preserved
1.7% of assistant prose. Two of the three target skill shapes — grounded
planning, execution checking — live *entirely* in that prose and were
undetectable in principle.

### Bugs that only live testing found

Four, all silent, none caught by the suite:

- `${CLAUDE_PROJECT_DIR}` **is not substituted** in a `.claude/commands/` file —
  the permission checker sees a live shell expansion and refuses. Use a relative
  path; the command runs from the project root.
- `allowed-tools` globs are **literal about spaces**: `Bash(python3 * skillpp *)`
  does not match `python3 bin/skillpp …`.
- A wrong absolute path was globbed as a session id and crashed in `pathlib`.
- **`transcript.stem[:8]`** — truncating a session id for a tidier provenance
  line. Real uuids diverge within eight characters, so it never showed up until
  two fixtures named `recurrence-a`/`recurrence-b` collided: the second read as
  already present, so **the count stopped rising with no error**. Now the full
  stem, with a regression test verified to fail against the old line.

---

## Testing

### Passed live, on real data

| | |
| --- | --- |
| extract candidates from a real session | imperative, instance-noise stripped |
| store + review written | frontmatter, provenance, both files |
| idempotence | "nothing new", nothing written |
| cross-conversation accumulation | two conversations, one `PATTERNS.md` |
| correct rejection of mundane work | *"standard git merge… Mundane git. Skip recording."* |
| barren session | `_No candidate found_`, bookmark still advanced |
| count merge | `seen 2×`, both sessions listed |
| body merge | every rule from both occurrences present |
| `/review-candidates` | showed it, asked, promoted with a written description |

`tests/fixtures/seed_recurrence.py` builds two snapshots of one conversation
doing the same procedure twice, the second hitting an obstacle the first did
not. It tests what unit tests cannot — whether the model *reaches for*
`--matches`, and whether a merge combines bodies rather than replacing.
Regenerate its `.jsonl` output; it is gitignored.

### Not yet tested — start here

**Does a promoted skill stop being re-proposed?** `prepare-session` does put the
`## Made into skills` section in the prompt, so the model can see it. Whether it
*uses* it is unverified. Needs a session doing work already promoted. **This is
the difference between a tool that learns and one that nags.**

**Body quality across several candidates.** The one promoted skill so far says
*don't regenerate, edit the pixels* but omits the venv bootstrap, the grid
overlay and the luminance-only transfer that the original review captured. A
skill you cannot follow is a note. One instance is not a pattern — watch the
next few before changing the prompt.

**`when_to_use` in practice.** Now written when supplied. Unverified whether
richer trigger phrasing actually improves firing.

---

## Open

**No rejection.** Decided: reject means delete, and the procedure gets a fresh
chance if it recurs. Not built. Consequence, accepted: nothing records the
rejection, so the same thing can be proposed again immediately — at `1×` it
sorts to the bottom, and `reviews/<session>.md` still holds the original.

**No `reconcile`.** Nothing notices when a promoted skill's file is deleted, so
the store keeps claiming it exists and the procedure is never re-proposed.

**No `expire`.** Candidates never decay. Dates are recorded, so this is
buildable whenever volume warrants.

**Re-promotion is blocked.** `promote()` refuses to promote twice, which is right
in general but means a description cannot be fixed by re-running
`promote-candidate --force`. Small rough edge.

**Automatic triggering is deferred, not rejected.** The `SessionEnd` hook still
enqueues to `ended-sessions.jsonl` and nothing drains it — deliberately, since
that queue is the evidence for whether manual triggering suffices. `claude -p`
works, ~$0.16 context floor, and **`--no-session-persistence` is mandatory** or
a drain creates a session, which fires the hook, which enqueues.

**The ledger is untouched and unused by this flow.** Adapting it was designed in
detail and rejected: `find_match` scores **0.00 on prose**, and its one-file-per-
entry shape is wrong for a model that must read every candidate at once to judge
a match. `skillpp review` / `promote` / `ignore` still operate on the old capture
path.

---

## Reference — Claude Code facts verified here

- **`` !`command` `` runs *before* the content reaches the model**, and its
  output replaces the placeholder. No exit-code branching, so "nothing to do"
  must be a sentence in the output.
- **The transcript JSONL structure is documented nowhere.** Only
  `transcript_path` is specified — hence `transcript.py` as a blast-radius
  boundary that raises rather than returning `[]` when a file has records but no
  recognisable messages.
- **`description` is what decides whether a skill fires**; `when_to_use` is
  appended to it in the listing, both read before the body loads.
- **A model will report a tool call it did not make.** Asked to write a file
  with `Write` unavailable, it printed "Wrote SKILL.md" and wrote nothing.
  Constrain with `--allowed-tools ""` rather than trusting the narration.

---

## Dead ends — do not retry

- **`similarity()` on prose** — splits on ` | `, scores **0.00 on every pair**
  including obvious matches. Structurally inapplicable.
- **`agent`-type hooks on `SessionEnd`** — `KP`'s signature accepts no
  `toolUseContext`; fails silently. `command` hooks work.
- **Code-based segmentation / fingerprint matching** — rated
  `edit:.md | bash:cd` a 178-occurrence workflow.
- **Lineage key + "last analysed UUID" cursor** — the key groups divergent
  branches, the pointer breaks when a resume rewrites its tail.
- **Per-conversation stores** — the reason counts could not accumulate.
- **Skipping the extractor** — the median *increment* is 443 KB raw, and 72% of
  increments exceed a ~30k-token budget.

---

## Prompt-design lessons already paid for

- **"Dead ends omitted" was wrong.** A task's failures *were* the reasoning.
- **A sharpening clause can become a loophole.** *"Would they only discover it
  by failing?"* let general shell trivia through. When adding a test to a
  prompt, check what it now admits, not only what it excludes.
- **Asking for a report gets a report.** The first working prompt produced
  *Intent / Steps / Worked / Did not work* — real findings, wrong shape. A skill
  is forward-facing instruction; ask for a SKILL.md, not a summary.
