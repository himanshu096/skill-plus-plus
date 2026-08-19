# Handover — session review (`feat/pattern-detection`)

Branch: `feat/pattern-detection`, clean tree.
**165 tests pass:** `python3 -m unittest discover -s tests -q`.
**7 evals pass:** `python3 tests/evals/run.py` — a few minutes and a few cents,
because each one spends a real model call.
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
                                                                 │  the store
/review-candidates    decide on one, write it out as a skill    ─┘
```

| path | what |
| --- | --- |
| `~/.claude/skillpp/patterns/<name>.md` | one candidate, written once, never reopened for writing |
| `~/.claude/skillpp/occurrences.jsonl` | one line per sighting; the count is how many there are |
| `~/.claude/skillpp/decisions.jsonl` | one line per human decision; the last one is the status |
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

## The store, and the two designs it replaced

**Per-conversation documents came first.** A count could only rise when one
conversation repeated a whole procedure — and 82% of conversations are a single
session, so counts sat at 1 and the ordering they were meant to drive did
nothing. Pooling every session is what makes recurrence visible.

**One `PATTERNS.md` came next, and was worse.** Its parser ended an entry at the
next `##`, and skill bodies use `##` for their own rules, so every body was cut
at its first rule. Because a write re-serialised the whole document, recording
against one entry destroyed the bodies of entries nobody had touched. The
append-only shape above is not a convention to be careful about — it is why that
failure cannot recur.

The same ambiguous-delimiter mistake then appeared a second time in the rewrite
(`text.split("\n---\n", 2)[-1]`, breaking on a body containing a horizontal
rule). **Assume a third.** Anchor the match; never take the last split.
- **The bookmark moved into `reviews/`.** Each review records `last_message` and
  `last_timestamp`, so there is no third place for state to live and disagree.
  A session that proposes nothing still writes one, or its messages are read
  again forever.
- **The bookmark lives in `reviews/`**, so there is no third place for state to
  disagree. A session that proposes nothing still writes one, or its messages
  are read again forever.
- **Status:** `candidate → promoted`, from the last decision logged.
- **A threshold, `memory.THRESHOLD = 3`.** Below it a recording is a log entry,
  not a question: `--open-only` filters on it and `/review-candidates` never
  offers it. What is recorded and what a person is asked about are separate,
  the same way status and evidence are.

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
| `memory.py` | the store: entry files, the two logs, derived counts, the threshold |
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

Three layers, and the split matters: **anything with one right answer belongs in
`tests/`, where it is free.** Only judgement is worth a model call.

### `python3 -m unittest discover -s tests -q` — 165, ~2s

Counts, dates, ordering, the threshold, and the four append-only properties
(round-trip with `##` sections, an occurrence not touching the entry file, a
neighbour left byte-identical, a body containing a horizontal rule).

### `python3 tests/evals/run.py` — 7 cases, ~4 min, a few cents

The model makes exactly two judgements. The cases are that 2×2, plus the
conditions that stress it:

| | is there a procedure? | is it one we have? |
| --- | --- | --- |
| **yes** | `new`, `big` | `match` |
| **no** | `barren`, `incomplete` | `distinct` |

`two` covers arity — a review that hands back the strongest and stops loses the
second silently, because the session is bookmarked as read either way.

`new` and `big` also assert body quality in the same call: no instance detail
(`OPS-4471`, the support URL, the exception class), and at least one of the
rules the run actually established.

Each case shells out to the **real** `/log-session`, sandboxed by `SKILLPP_ROOT`
alone. An eval that reconstructs the prompt tests the reconstruction, and the
two drift the first time someone edits the command file.

`big` is 1,879 records / **433 KB raw** — the median real increment, not a toy —
extracting to ~16k tokens. Everything between the extractor and the judgement
was untested at that size before it.

**Do not assert on vocabulary.** The first `two` check required the word
"ticket" and failed a correct body that said "issue" and "tracker" throughout.
A flaky eval gets muted, and a muted eval is worse than none.

### Found by the evals, not by the suite

`prepare-session` printed `store  <root>/patterns`. The model read that path and
passed it back as `--root`, building a complete second store at
`patterns/patterns/` with every count restarting from zero and **nothing
reporting a problem**. Nothing was wrong with the code — only with what the code
told a model. Fixed by printing `config.root`, which is the path the flag
actually takes.

### Not yet tested

**`/review-candidates` end to end.** `AskUserQuestion` needs a human; automating
it means stubbing the tool and testing the stub. The queue-filtering half is a
unit test.

**Cold discovery.** Whether a promoted skill fires unprompted — the real test of
`description` and `when_to_use`. Needs a different harness and something
promoted.

**Above the budget.** 72% of real increments exceed ~30k tokens; `big` sits at
16k. Whether `prepare-session` truncates or floods at 2× that is unknown.

---

## Open

**No rejection.** Decided: reject means delete, and the procedure gets a fresh
chance if it recurs. Not built. Consequence, accepted: nothing records the
rejection, so the same thing can be proposed again immediately — at `1×` it
sorts to the bottom, and `reviews/<session>.md` still holds the original.

**A promoted skill's file goes stale, and updating it is deliberately deferred.**
Measured after the promoted-match test: the store held `seen 2×`, both sessions
and a seven-section merged body, while the live
`~/.claude/skills/<name>/SKILL.md` still read `seen: 1`, `["recurrence-a"]` and a
single paragraph. `record-candidate --matches` updates the store only, and
`promote()` refuses a second promotion, so **everything learned after promotion
accumulates where no agent reads it.** The store gets smarter; the skill does not.

Not a truncation bug in `promote-candidate` — it writes `entry.body.strip()`
verbatim (`cli.py`, in `cmd_promote_candidate`). **Unexplained, and worth a look
before building the refresh:** A's review recorded five `##` sections, yet the
promoted file has only A's opening paragraph. Either the store body at promotion
time was already just that paragraph, or something between the review and the
store dropped the sections. Do not build a refresh path on the assumption the
merge is sound until that is understood.

Same mechanism as the earlier observation about a promoted skill omitting the
venv bootstrap and grid overlay — two instances now, so it is a pattern.

Decided: **skip for now.** When it is picked up, the real question is not
mechanical (`--force` already exists) but whether a skill's text may change
under the developer without review, which cuts against "promoting one is a
separate deliberate act". A middle option is to flag drift in
`/review-candidates` and let a person refresh.

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
