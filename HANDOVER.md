# Handover — session review (`feat/pattern-detection`)

Branch: `feat/pattern-detection`, clean tree, **19 commits ahead of
`origin/feat/pattern-detection` and not pushed** (origin is at `6572b44`).
**207 tests pass:** `python3 -m unittest discover -s tests -q`.
**37 eval cases.** `python3 tests/evals/run.py` spends a real model call per
case, so run `--case <name>` unless a full sweep is actually wanted. Which cases
are verified and which are stale is in *Testing*.
No `pyproject.toml`; use `uv run --with pytest pytest tests/` if you want pytest.
Invoke the CLI as `python3 bin/skillpp`.

Numbers marked *measured* come from the transcripts on this machine: ~117 files,
~324 MB, six projects, about 60 days.

**Detection is built and live-tested end to end**, and was compared head to head
against the competing design on `feat/ignore-list-and-drift-tracking`: this
branch won 5 of 6 against 2 of 6 on six sessions built from *that* branch's own
scenario table. Method and per-scenario results in `docs/bakeoff.md`. Treat the
verdict as settled.

**The current work is getting inference onto a local model**, step by step, by
making the prompt smaller rather than by swapping the model. `docs/windowing.md`
is the live document for it. Short version: compression is spent — twelve real
sessions run 60k-128k tokens after a 47x extract, and every remaining cut summed
to about 5% — so the prompt is *split* instead, and `window.py`, `reconcile.py`
and `/locate` are that pipeline. Nothing local has been tried yet; the standing
instruction is not to until the frontier arm is clean.

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
- **The bookmark lives in `reviews/`.** Each review records `last_message` and
  `last_timestamp`, so there is no third place for state to live and disagree.
  A session that proposes nothing still writes one, or its messages are read
  again forever.
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
| `window.py` | messages → segments (one request and its work) → windows that fit a small context. Also `keep_aspects`, which shows one aspect of a request at a time |
| `reconcile.py` | per-window findings → groups, deciding from segment provenance which are duplicates, which are recurrences, and which need a judgement |

And the tests, which are two different things and should stay that way:

| path | job |
| --- | --- |
| `tests/test_skillpp.py` | everything with one right answer. Free, ~2s |
| `tests/evals/run.py` | the judgement cases. One real model call each |
| `tests/evals/sessions.py` | fixtures the recurrence seeder cannot express |
| `tests/evals/mundane.py` | a session worth no skill at all |
| `tests/evals/their_scenarios.py` | the capture branch's own six scenarios, built on its turf, for the symmetric round |
| `tests/evals/replay.py` | a transcript replayed as the hook stream it would have produced, so that branch's capture can be driven from a fixture |
| `tests/evals/bakeoff.py` | runs every fixture through that branch's detector. **No model calls** — its detection is code |
| `tests/evals/locator.py` | fifteen labelled segments for `/locate`. The prompt was revised against these, so they are tuned |
| `tests/evals/cold.py` | four fixtures the `/locate` prompt was never tuned against, labelled before first run. **Do not revise a label because an answer disagrees** |
| `tests/fixtures/seed_recurrence.py` | two snapshots of one conversation doing the same procedure twice. Regenerate; the `.jsonl` is gitignored |

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

### `python3 -m unittest discover -s tests -q` — 167, ~2s

Counts, dates, ordering, the threshold, and the four append-only properties
(round-trip with `##` sections, an occurrence not touching the entry file, a
neighbour left byte-identical, a body containing a horizontal rule).

### `python3 tests/evals/run.py` — 13 cases, minutes and cents each

Each case shells out to the **real** command file, sandboxed by environment
alone. An eval that reconstructs the prompt tests the reconstruction, and the
two drift the first time someone edits the command file.

**Detection.** The model makes exactly two judgements, and the cases are that
2×2 plus what stresses it:

| | is there a procedure? | is it one we have? |
| --- | --- | --- |
| **yes** | `new`, `big` | `match`, `promoted` |
| **no** | `barren`, `incomplete` | `distinct`, `near` |

- `two` — arity. A review handing back the strongest and stopping loses the
  second silently, since the session is bookmarked as read either way.
- `stop` — the same session reviewed twice. Must obey "write nothing" rather
  than re-record and inflate the count without the procedure recurring.
- `secrets` — a finished procedure whose commands carry a token. A leaked
  credential in a SKILL.md travels further than the transcript ever would,
  because that file gets committed and shared.
- `near` is the one that matters more than `distinct`. `distinct` only proves
  the model will not match things with *nothing* in common; `near` seeds a
  different procedure from the same domain, which is where a false match
  actually happens. A missed match costs a visible duplicate; a wrong match
  inflates a count, drops the proposal, and leaves no trace.
- `new` and `big` assert body quality in the same call, so it is free: no
  instance detail, and at least one rule the run established.
- Every case asserts a bookmark was written. An empty store looks identical
  whether a session was reviewed and dismissed or never reviewed at all.

`big` is 1,879 records / **433 KB raw** — the median real increment, not a toy —
extracting to ~16k tokens.

**Review.** `AskUserQuestion` is withheld deliberately: everything up to the
question is observable, and answering it would mean stubbing the tool and
asserting on the stub.

- `review-empty` — everything below the threshold. Nothing promoted, no
  SKILL.md, store unchanged. The threshold gates the listing; this is what
  proves it also gates the promotion.
- `review-lists-all` — three past the threshold, all three named in the output,
  nothing written before an answer exists.

**Do not assert on vocabulary.** The first `two` check required the word
"ticket" and failed a correct body saying "issue" and "tracker" throughout. It
compares word overlap between entries now. A flaky eval gets muted, and a muted
eval is worse than none.

### Found by the evals, not by the suite

Three, none reachable by a unit test, because in each the code was correct and
what it *told a model* was not:

- **`prepare-session` printed `store  <root>/patterns`.** Handed that path, the
  model passed it back as `--root` and built a second store at
  `patterns/patterns/`, every count restarting from zero, nothing reporting a
  problem. Prints `config.root` now — the path the flag actually takes.
- **Promotion escaped the sandbox.** `default_skills_dir()` falls back to
  `~/.claude/skills`, and this repo has no `.claude/skills/`. `SKILLPP_ROOT`
  redirects the store, but promotion writes *outside* it, so a review case that
  wrongly promoted would have landed a SKILL.md in the developer's real skills
  directory. `SKILLPP_SKILLS_DIR` now overrides both, which is what the config
  docstring already claimed. Two unit tests pin it.
- **A refused tool call read as a wrong judgement.** `near` failed once with
  the model's reasoning visibly correct and both Bash calls denied — the store
  stays empty either way. The runner detects refusals and reports them as
  harness failures now.

### Harness notes

`claude -p` prints the **final message only**. `review-lists-all` first failed
with the model reporting "leaving all three as candidates" — it *had* listed
them, but by the last turn that was summarised away. The runner parses
`--output-format stream-json` and keeps every assistant turn.

Verified since the last full sweep: `near`, `promoted`, `secrets`, `stop`,
`review-empty`, `review-lists-all`. The original seven passed before the
universal bookmark check, the streamed output and `SKILLPP_SKILLS_DIR` landed,
and have not been re-run against them.

### The three that matter

Everything else in the eval suite is secondary to these, and it is worth knowing
which case covers which before adding a thirty-eighth.

| the claim | covered by |
| --- | --- |
| the model notices a procedure in a session | eval `new` |
| it recognises one already recorded and the count rises | eval `match`, guarded by `distinct`, `near`, `promoted` |
| a candidate past the threshold, once accepted, becomes a skill on disk | `TestTheWholeLoop` |

The third had nothing until it was written. `review-lists-all` stops at the
question a person answers and the accept-path tests call `promote-candidate` on
a seeded store, so both ends were covered and the join was not.
`TestTheWholeLoop` walks the whole chain through the real CLI with nothing
seeded: three distinct sessions record it, the count reaches three, the queue
offers that name, promotion writes the SKILL.md, and a fourth sighting afterwards
raises the count without filing a duplicate. The only thing left out is pressing
the button, which `claude -p` cannot do.

Worth keeping in proportion: 24 of the 37 eval cases are `locate-*`, `their-*`
and `cold-*` — the local-inference track and the cross-branch comparison. Those
serve components that are not in the pipeline. The three above are the product.

### Not yet tested

**The queue is now drainable, and had never been drained.** The project
`SessionEnd` hook has been appending every ended session to
`.claude/skillpp/memory/ended-sessions.jsonl` for weeks: 159 queued, 2 reviewed,
157 not. So the trigger was never the missing piece — the drain was. `skillpp
drain` reads the queue, skips what is reviewed, and is a dry run unless
`--apply`, since each application is a model call.

Two facts it exists to survive. **143 of the 157 unreviewed entries point at a
transcript that no longer exists** — the parent directories are there with 24
files each, and none of the 143 are anywhere on disk. Whether they were pruned
or never wrote a transcript cannot be told after the fact, but a queued path can
be dead by the time anyone looks, so draining promptly is strictly safer than
sweeping later. And a drain that spawns `claude -p` **without**
`--no-session-persistence` creates a session that ends, which the hook enqueues,
which the next drain picks up: the queue refills itself faster than it empties.
There is a test asserting the flag is in every command the dry run prints.

That leaves 14 sessions drainable right now, 13 of them large enough to clear the
message floor. Draining them is the first time the store would have real
accumulated input, which is what the 3x threshold needs before
`/review-candidates` has ever had anything to offer.

**Anything local.** `window.py`, `reconcile.py` and `/locate` exist to put the
cheap pass on a local model and none of it has met one. `qwen2.5:7b-ctx16k` and
`gemma4:12b-ctx16k` are installed. The standing instruction is not to try until
the frontier arm is clean.

**Nothing calls `window.py` or `reconcile.py`.** Both are libraries with tests
and no caller; `prepare-session` still renders a whole session. Wiring them in
is what changes the input a model sees, and the eval to run at that point is
`big` — the `their-*` and `locate-*` fixtures are 200-300 tokens and produce one
window each, so windowing is a no-op on them.

**Cold discovery.** Whether a promoted skill fires unprompted — the real test of
`description` and `when_to_use`. Needs a different harness and something
promoted; both promoted skills were cleared to give the threshold a clean run.

**Above the budget.** 72% of real increments exceed ~30k tokens; `big` sits at
16k. Whether `prepare-session` truncates or floods at 2× that is unknown. The
census in `docs/windowing.md` is the sharper number: 14 of 14 real sessions
exceed a 16k context after extraction, at 60k-128k tokens.

The accept path **is** covered now — `TestTheAcceptPath`, eleven tests against
`promote-candidate`, including that the entry file is byte-identical after a
promotion.

---

## Open

**No rejection.** Decided: reject means delete, and the procedure gets a fresh
chance if it recurs. Not built. Consequence, accepted: nothing records the
rejection, so the same thing can be proposed again immediately — at `1×` it
sorts to the bottom, and `reviews/<session>.md` still holds the original.

**A promoted skill's file goes stale, and refreshing it is deferred.** A match
logs a sighting and nothing else — the stored body is what the *first*
occurrence taught, and later accounts live in their own `reviews/<session>.md`.
So a procedure can recur ten times and neither the entry nor the promoted
SKILL.md learns anything from nine of them. Deliberate: the alternative is a
body rewritten on every match, which is exactly the write pattern that
corrupted the old store.

Explained, and now closed: the earlier note here called it *unexplained* that a
review recording five `##` sections produced a promoted file holding only the
opening paragraph. That was the `SECTION_HEAD` parser ending an entry at the
next `##`. It is gone with the document, and a test pins bodies with sections
through a round trip.

When the refresh is picked up, the question is not mechanical (`--force`
exists) but whether a skill's text may change under the developer without
review, which cuts against "promoting one is a separate deliberate act". Middle
option: flag drift in `/review-candidates` and let a person refresh.

**The store on this machine is deliberately empty of promotions.** Both promoted
skills were test artifacts and were cleared, along with
`PATTERNS.md.superseded`, so the threshold gets a clean run. Three candidates
remain (`x2`, `x2`, `x1`); nothing reaches the queue until something hits `x3`.
Cold-discovery testing is blocked until something is promoted again.

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

**Every auxiliary hint gets promoted to a verdict.** `/locate` has been revised
four times and each revision removed a hint of mine that the model had used as
sufficient on its own. "A command list that ends in a read is usually not
landed" marked a finished procedure unresolved, because its mutations sat
mid-list with unrelated greps after them. "The developer accepted it and moved
on" made a request that merely *extended* the same change read as acceptance.
"Or the developer moved on to different work" made a question answered from one
grep read as finished work.

The pattern is not that the hints were badly worded. It is that a hint offered
as corroboration is read as a rule. State the condition that actually decides —
for `landed`, that something was carried out *and* confirmed, both halves
required — and give the auxiliary signals only as things that corroborate it,
saying explicitly that none of them suffices alone.

**A verdict the model cannot answer consistently is usually two questions.**
`/locate` began with three verdicts and every disagreement it produced was
between `none` and `open`, never involving `landed`. Reading that is the first
half of a procedure is both "nothing happened" and "unresolved" at once.
Collapsing them fixed the inconsistency, and cost something worth knowing: the
surviving `open` now means two things, and the prompt has to say what unites
them rather than define them.



- **"Dead ends omitted" was wrong.** A task's failures *were* the reasoning.
- **A sharpening clause can become a loophole.** *"Would they only discover it
  by failing?"* let general shell trivia through. When adding a test to a
  prompt, check what it now admits, not only what it excludes.
- **Asking for a report gets a report.** The first working prompt produced
  *Intent / Steps / Worked / Did not work* — real findings, wrong shape. A skill
  is forward-facing instruction; ask for a SKILL.md, not a summary.
