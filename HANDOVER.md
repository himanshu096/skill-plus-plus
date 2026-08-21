# Handover — session review (`feat/pattern-detection`)

Branch: `feat/pattern-detection`, clean tree, **4 commits ahead of origin and
not pushed** (origin is at `9258fdc`).
**275 tests pass:** `python3 -m unittest discover -s tests -q`.
**40 eval cases.** `python3 tests/evals/run.py` spends a real model call per
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

---

## Fixed 2026-08-21: a session did not need another session to look like itself

`nearest_session` compared this session's text against every stored exemplar
with no guard against one written earlier **in the same run**. Every
`record-candidate --auto-match` call in one `/log-session` embeds the same text
— a session's new messages do not change between calls — so a second candidate
was compared against an exemplar that same sequence had just written from
identical text. It could not help but match.

**Detection was never wrong; matching silently undid it.** On
`two-chores-one-sitting` the model wrote two correct, distinct bodies and the
merge collapsed them at a score logged as `0.603` — below every threshold in
`similar.py`. That was the *body* comparison; the *session* comparison, which
actually decided it, scored **1.000** and was overwritten in the log by
whichever check ran second.

Fixed by `exclude_session`, and by keeping both reasons rather than one
clobbering the other. **0 of 3 multi-task cases split correctly before, 3 of 3
after.** `TestAutoMatchWithinASession` reproduces it end to end.

Found only because a benchmark written by someone else claimed this branch
banks exactly one candidate per session. It did — for a reason nobody here had
looked for.

---

## What 2026-08-21 established, part two: run against the other branch

`feat/episode-filter` was run directly — its worktree, its model
(`gemma3n:e4b`, pulled), its defaults — over **four real sessions from this
machine**. Scoring **detection only**: both branches hand the body to the same
frontier model, so draft quality measures Claude, not either design.

| session | this branch | theirs, shipped defaults |
| --- | --- | --- |
| three test-runner sessions | 1 skill, promoted, verified firing | **0** — one procedure split into three entries at ×1 |
| `ac68cada` (UI work, 1028 tools) | 2 candidates | **0** — 32 entries, sift kept 2, draft declined both |
| `54b0e6cf` (email + doc audit) | 2 candidates | **0** — procedure trimmed before matching ran |
| `610b2d40` (ROS/ML, 712 tools) | 3 candidates | **0** — 32 entries, 0 ready |

Their **recall was never the problem**: `docker`, `reset --hard` and `EOF` all
sit in their ledger for the session this branch drew three procedures from.
Recognition is.

### Two independent failures, both measured on their code

**No viable similarity threshold exists.** Their signature is command-shaped
(`bash:python3 | bash:ls`) and merges above `SKILLPP_SIMILARITY`, shipped at
0.85. Two ordinary sessions demand contradictory values:

| session | requires | why |
| --- | --- | --- |
| test-runner | **< 0.460** | one procedure, three sightings, scoring 0.460–0.503 against each other |
| UI work | **> 0.703** | *"I have figured out a new onboarding journey"* scores **0.703** against *"my previous chat was interrupted"* |

`0.460 < 0.703`, so the window is empty. At 0.85 the first splits; at a tuned
0.45 the second falsely merges three unrelated tasks. Command shape is not
identity. The same decision by embedding (`similar.py`) separates at
0.848–0.873 versus 0.513 — a gap of ~0.3 against a window here that is
negative.

**Read-only work is invisible.** The email procedure is `git log`, `git
status`, `git diff`, `git branch` — every one classified read-only, so the
leading-exploration trim removes the whole episode, and with no `git commit`
there is no completion marker either. Invisible twice over, and unreachable by
tuning: nothing survives to be matched. A procedure whose product is *prose*
leaves no mutating command.

### Taken from them, because it was better

- **The step trace** (`ce906df`). They keep what literally ran; this branch
  kept only the generalised body. That is the missing input for redrafting a
  body that turns out wrong — the `PYTHONPATH` case — without going back to
  the transcript.
- **Their input whitelist**, ported with it, and the reason their commands stay
  parseable. Raw input gave a 621 KB trace holding whole file bodies and both
  sides of every edit, **unscrubbed** — a credential in an edited file went to
  disk verbatim. Now 255 KB, paths only for writes, everything through
  `scrub_obj`.
- **Negative triggers and `requires_cli`** (`ad953ca`). Their draft carries a
  *"do not use this skill when"* list and declares its dependencies. Both are
  prompt design rather than detection, and worth having regardless.

### Three claims from this comparison that were wrong

Recorded because each was stated with confidence before being checked:

1. *"Their pipeline produces zero skills."* Tuned to 0.45 it drafted a good
   SKILL.md and independently named it `bootstrapping-an-ephemeral-test-runner`
   — the same name this branch chose.
2. *"`similarity()` scores 0.00."* That number came from reading a
   `signature:` key that is derived, not stored.
3. *"0.45 works."* Derived from the ledger it was then tested on, and it
   false-merges on the next session.

### Using their harness correctly

`replay.py` sends one `Stop` per assistant turn. Their dispatcher routes
`Stop` to the same handler as `SessionEnd`, so every turn ended the session and
every span folded at one step — nothing banked at all. Their installer
registers only `("UserPromptSubmit", "PostToolUse", "SessionEnd")`, so no
`Stop` hook exists in deployment and suppressing it reproduces their real path.
**A genuine `replay.py` incompatibility, worth telling them about.**

---

## What 2026-08-21 established

Detection, discovery and verification are three different questions, and until
today only the first had a test. A skill can be detected correctly, fire
correctly, and still be wrong.

| | the question | cases |
| --- | --- | --- |
| detection | is there a procedure here worth recording? | `new`, `barren`, `match`, `distinct`, `near` |
| discovery | does the promoted skill get found and reached for? | `discovery-fires`, `discovery-quiet` |
| verification | followed as written, does the work actually succeed? | `verify` |

### Cold discovery works, confirmed by hand

`bootstrapping-an-ephemeral-test-runner` fired unprompted in a throwaway
project that had never seen the original work — first action, from "Run this
project's tests", nothing naming it. That closes the last link the loop had
never been observed doing outside a harness.

`discovery-quiet` is the half that earns its keep. Without a case proving the
skill *stays quiet* on a request it does not cover, a description matching
everything passes the fire test. Over-firing is not harmless: it loads a body
on unrelated requests and teaches the developer to ignore skills.

### The first promoted skill was already wrong

It fired, ran its prescribed command, hit a pytest collection error, recovered
with `PYTHONPATH=.`, and reported success. Every naive check passes that run.

This is the asymmetry worth internalising: a **missing** skill costs nothing
that was not already missing, while a **wrong** one fires in every matching
session from now on and the agent trusts it rather than reasoning from scratch.
Blast radius scales with promotion count, silently. At the time of writing the
defect rate on promoted skills is one for one.

### Assert on retries, not outcomes

Two assertions were tried and were wrong before this one:

- **Exit codes**, wrong in both directions. Too strict: the skill's own probe
  `which uv pipx pip3` exits 1 whenever one is absent, which is the probe
  working. Too lax: the test command ends `2>&1 | tail -80`, so the pipeline
  exits 0 even when collection fails — hiding the only real defect.
- **Substring matching over every command**, which called `ls dir` followed by
  `ls dir/tests` a retry. Browsing is not retrying.

What survives: if the agent runs a command and then runs that same command with
something added, the first attempt did not do the job, and **what it added is a
rule the skill should have carried**. Plus fired, finished, and left no `.venv`
— the last being the skill's own promise about itself.

Proven by use: `verify` failed, the rule moved from recovery into prevention,
`verify` passed. The first edit taught the skill to *recover* from the error
rather than *prevent* it, and only the retry check could tell the difference.

### The harness blames the model for its own problems

Four times now an environment failure was reported as a wrong judgement: tool
denials, an expired OAuth token, permission refusals counted as failed calls,
and a check reading assistant prose where it needed the raw event stream. Each
would have read as "the model got it wrong".

Assume a fifth. Environment failures now carry a `HARNESS:` prefix, and the
fixes carry their reasons in the code.

### Usage tracking is gone, not fixed

`record_use` was only ever called from a `PostToolUse` hook that is not
installed, so `skillpp lifecycle` stamped "never used" on every skill —
including one demonstrably firing on demand. A column that can only report zero
reads as a measurement, so the fields went with the writer.

Nothing was lost. **An invocation is already in the transcript.** Both usage
counts and last-used dates are a `json.loads` away, and deriving them cannot
disagree with the record the way a second copy can. No hook is needed to know
whether a skill is used.

---

## What 2026-08-20 established

The day started with detection built and never observed working, and ended with
the loop closed on real history and three of its four steps running locally.
40 commits, 254 tests, 37 eval cases, pushed through `2fdd7f1`.

### The loop closed, on real sessions

`bootstrapping-an-ephemeral-test-runner` was detected in one of this machine's
sessions, recognised in two more, reached the threshold, was offered, accepted,
and is now a live skill in `~/.claude/skills`. Every stage of the chain has now
happened for real rather than in a harness:

    SessionEnd fired -> queued -> drained -> detected -> matched -> matched
    -> x3 -> offered -> accepted -> SKILL.md written -> discovery lists it

The trigger was never the missing piece. The `SessionEnd` hook had been
appending for weeks — 159 queued, 2 reviewed — and nothing had ever read the
file. `skillpp drain` is that reader.

### Local does three of the four jobs

| step | who | evidence |
| --- | --- | --- |
| below the message floor | code | 6 of 18 queued sessions, free |
| is this session worth reading | `qwen2.5:7b` | 12 real sessions: 6 of 6 with a procedure sent on, **0 lost**, 4 of 6 empty skipped |
| is this the same procedure | `nomic-embed-text` | merged at 0.848 and 0.873 under two *different* proposed names; declined an unrelated body at 0.513 |
| write the body | frontier | measured out of reach — see below |

Nothing outside `config.py` names a vendor. `SKILLPP_AGENT` is a command
template, and the body is written by whatever agent runs `/log-session`, which
is a prompt in a markdown file rather than an API call.

### Four walls, measured rather than assumed

Written down so the next person does not spend a day rediscovering them.

- **A 7B cannot write a SKILL.md body**, one call or four decomposed calls. It
  transcribes the run instead of generalising, names the repository it happened
  to touch, and picks procedures the frontier judge rejected.
- **A session cannot be matched against a body.** It sits near-equidistant from
  every body in the store, a spread of 0.07. Not the model, not nomic's task
  prefixes, not the granularity — comparing across registers is the problem.
  Staged command matching scored 0/4, 1/4 and 1/7.
- **Compression cannot fit a session into a local context.** 14 of 14 real
  sessions exceed 16k after a 47x extract; every remaining cut summed to ~5%.
  143 chars per line is already terse — the sessions are simply long.
- **The span pass filters nothing.** 4%, 0% and 0% reduction on three real
  sessions, because a partition cannot reduce. `window.py`, `reconcile.py` and
  the locators stay unwired for that reason: they were built to feed a local
  writer that does not exist.

### The competing design, settled

`feat/ignore-list-and-drift-tracking` was scored on six sessions built from its
own scenario table, in its own vocabulary, inside its own budgets: **5 of 6
against 2 of 6**. Its span logic fragments a procedure at every gate it passes,
its recurrence counting never fires, and a long session banks either nothing or
one 440-step blob. Full method in `docs/bakeoff.md`. Treat as settled.

### Lessons that cost the most to learn

- **Every auxiliary hint in a prompt gets read as a rule.** Seven revisions of
  `/locate`, each one removing a hint of mine the model had used as sufficient
  on its own. And the sharper form: *the hints a frontier model needs are the
  ones a 7B over-applies* — the next-request peek took the judge from 20/21 to
  21/21 and the 7B from 18/21 to 16/21. Two audiences need two prompts.
- **A decision code can make must not go to a model** just because a model is in
  the loop. Two instances: the drain spent five calls to be told sessions were
  below the floor, and triage was asked about sessions containing no request at
  all.
- **Toy fixtures hid three separate defects** — a truncation, a boundary
  placement, and the filtering — all of which appeared on the first real
  session. Fixtures were 102 tokens with one procedure and no follow-up; real
  segments run 869 median and 3,670 at p90.
- **Test before building.** The last four builds each went one step past the
  evidence. The one where the test came first cost ten minutes instead of an
  hour.

### Still open

- **Cold discovery.** Whether a promoted skill fires unprompted. No harness
  reaches it; only use answers it.
- **Thin evidence everywhere local.** n=3 on triage negatives, n=4 body pairs,
  n=15 session pairs with a 0.004 margin. All of it grows on its own —
  `truth.py` reads labels from the reviews the pipeline writes, and exemplars
  accumulate per sighting.
- **The prediction to watch.** The session-matching margin should widen as a
  procedure recurs, since scoring takes the best of several sightings. Nothing
  tests it; only future sessions can.
- **`drafting-a-repo-status-update-email` sits at x2.** One more sighting takes
  it to x3 — the second promotion, and the first from a store that filled up on
  its own rather than from a backlog.

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
| `~/.claude/skillpp/reviews/<session>.steps.jsonl` | what that session literally ran, scrubbed — the input for redrafting a body |
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

### `python3 -m unittest discover -s tests -q` — 259, ~2s

Counts, dates, ordering, the threshold, and the four append-only properties
(round-trip with `##` sections, an occurrence not touching the entry file, a
neighbour left byte-identical, a body containing a horizontal rule).

### `python3 tests/evals/run.py` — 40 cases, minutes and cents each

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

**Discovery and verification.** These three run against the *live*
`~/.claude/skills`, where Claude Code actually reads — `SKILLPP_SKILLS_DIR` is
no help, that being where skillpp writes. Each builds a throwaway project and
runs with `cwd` there.

- `discovery-fires` — a bare Python project and "run the tests", nothing naming
  the skill. Asserts it was reached for.
- `discovery-quiet` — a request the skill does not cover. Without it, a
  description matching everything passes the fire test.
- `verify` — fired, finished, no retry of its own command, and no `.venv` left
  behind. The retry check is the load-bearing one; see *What 2026-08-21
  established* for why outcomes and exit codes both failed as assertions.

They read the promoted SKILL.md by path, so they test the wording that actually
shipped. Delete that file and all three fail on a missing path rather than
telling you anything.

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

### Verified for the current configuration

Frontier writes the body, local does everything else. Both matching paths have
evidence, and they were exercised separately rather than one masking the other.

| path | verified by | adversarial case included |
| --- | --- | --- |
| session against session (`nearest_session`) | a real drain: merged at 0.848 and 0.873 under two *different* proposed names, declined an unrelated body at 0.513 | yes — the 0.513 |
| body against body (`closest`, the fallback) | eval cases `match`, `distinct`, `near`, `promoted`, `stop`, `their-recurs`, 6/6 | yes — `distinct` and `near` both require refusing a merge |
| local triage | 12 labelled real sessions: 6 of 6 sent on, 0 lost, 4 of 6 skipped | the 0-lost is the point |
| the whole loop | a sandboxed drain to promotion: 3 sessions, one entry at x3, offered, accepted, SKILL.md on disk | — |

The eval seeds write entries and occurrences but no exemplars, so
`nearest_session` finds nothing there and falls through to body comparison. That
is why the seeded cases test the fallback and the real drain tests the primary
path; neither substitutes for the other.

The run worth repeating for the division of labour, from one drain of three:

    below the floor 5   free, no model at all
    triaged out    2    local model
    matched        2    local embeddings, 0.848 and 0.873
    declined       1    local embeddings, 0.513
    bodies written 3    frontier

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

Worth keeping in proportion: 24 of the 40 eval cases are `locate-*`, `their-*`
and `cold-*` — the local-inference track and the cross-branch comparison. Those
serve components that are not in the pipeline. The three above are the product.

### Not yet tested

**The queue is now drainable, and had never been drained.** The project
`SessionEnd` hook has been appending every ended session to
`.claude/skillpp/memory/ended-sessions.jsonl` for weeks: 159 queued, 2 reviewed,
157 not. So the trigger was never the missing piece — the drain was. `skillpp
drain` reads the queue, skips what is reviewed, and is a dry run unless
`--apply`, since each application is a model call.

**Most of that queue was never real work.** 158 of 174 entries pointed at a
transcript that does not exist, and the cause is not pruning: a `claude -p` run
ends like any session and fires the `SessionEnd` hook, but
`--no-session-persistence` means it writes no transcript. So every eval sweep and
every drain call left a dead entry. `--no-session-persistence` stops the
transcript, not the hook — which is worth knowing, because the earlier note here
claiming it prevents re-enqueueing was wrong.

Fixed at the source: the hook now skips a session whose transcript is not on
disk, so automation stops queueing itself. `skillpp drain --prune` clears a
backlog; it took 174 entries to 16.

**The loop has now run end to end on real history.** Drained 15 sessions:
`bootstrapping-an-ephemeral-test-runner` was detected in one, matched in two
more, reached x3, was the only candidate `/review-candidates` offered, was
accepted, and is now a live skill at
`~/.claude/skills/bootstrapping-an-ephemeral-test-runner/SKILL.md` — it appears
in the skill list of sessions in this repo. Claims one, two and three are
observed facts rather than eval assertions.

Precision on the rest was good and is weak evidence: of the last 10 drained, 5
stopped at the message floor and 5 were reviewed with nothing recorded, declining
sessions that were skillpp fixture-testing, skillpp dogfooding, or themselves a
`/log-session` run. Correct answers, but those sessions are unusually meta — a
fairer test needs sessions doing ordinary work.

**Where the local model sits, exactly.** Detection is Claude. The loop that
closed — a procedure reaching x3 and becoming a live skill — was Claude at every
step that needed judgement. `qwen2.5:7b` does one job: `skillpp drain --triage`
asks it whether a session is worth a frontier call at all, and skips the ones
that are not. Measured on twelve sessions the pipeline had already reviewed: 6 of
6 with a real procedure sent on, 0 lost, 4 of 6 empty ones skipped, 29s, free.

Opt-in, and it fails toward spending. An unreachable Ollama, a missing model, or
an unclear answer all return "send it on", so a machine without a local model
behaves exactly as it does today. A saving that loses work is not a saving, and a
skipped session is never revisited.

Two bugs found by running it rather than reading it, both in the first real
invocation: it triaged the whole transcript instead of the new-since-bookmark
slice the judge reads, and it treated "few developer prompts" as "nothing
happened", which skipped a session with 73 new messages. It now reads the judge's
slice and falls back to the whole slice when the requests alone are too thin.

**What runs locally, and what the measurement said could not.** Two of the
three things detection does are local now; the third is the artifact itself.

| step | who | evidence |
| --- | --- | --- |
| is this session worth reading | `qwen2.5:7b`, `drain --triage` | 6 of 6 real procedures sent on, 0 lost, 4 of 6 empty skipped |
| detect the procedure and write the body | Claude | a 7B fails this one call or four; see `skillpp/prompts/README.md` |
| is this body already in the store | `nomic-embed-text`, `record-candidate --auto-match` | same procedure 0.873-0.990, different 0.569-0.776, threshold 0.824 |

`skillpp/similar.py` is the matching half. It removes the only judgement here
that scaled with the store: the model used to be handed every recorded candidate
to compare against, so the prompt grew forever, and a dot product per candidate
does not. It falls through to "new" when Ollama is unreachable, which is the safe
direction — a duplicate can be merged by a person later, while a wrong merge
raises someone else's count and discards a proposal with nothing recording it.

**Two comparisons, because a session and a body do not compare.** A session sits
near-equidistant from every written body in the store — a spread of 0.07 across
all of them — and neither nomic's task prefixes nor comparing against the trigger
sentence alone changed that. The signal does not survive crossing registers.

Session against session does separate, so every sighting's session text is
embedded and appended to `exemplars.jsonl`, and a new session is compared against
the best sighting of each procedure. The gap there is 0.004 wide — 0.723 for the
closest different pair, 0.727 for the furthest same pair — so it has three zones
rather than a threshold: act above `SURE_MATCH`, act below `SURE_NEW`, hand the
middle to the frontier model. On three real sessions that gave one correct match,
one correct new, and one honest deferral. There is a test pinning the boundaries
to the measured numbers, because they are not round figures.

The margin should widen as procedures recur, since a session is scored against
the best of several sightings rather than one. That is a prediction and nothing
here tests it — only future sessions can.

`locate` and `window.py` remain unwired: both were built to feed a local writer,
and a 7B cannot write a body.

**Nothing names a vendor outside `config.py`.** The distinction worth
understanding before changing any of this:

The **skill body is written by whatever agent runs `/log-session`**, which is a
prompt in a markdown file rather than an API call. A Cursor user's model writes
it in Cursor and skillpp never knows which model it was. There is nothing to
configure and hardcoding one would be strictly worse.

What did need configuring is the **unattended** path. Draining a queue means
starting an agent nobody asked for, and both the binary and its flags are
host-specific — `--no-session-persistence` is mandatory for Claude Code or the
drain re-queues itself, and `--allowed-tools` has no equivalent elsewhere. So
`SKILLPP_AGENT` is a command template with a `{prompt}` placeholder, defaulting
to the Claude Code invocation. A missing binary reports what to set rather than
raising.

Also overridable now, per this module's own promise: `SKILLPP_OLLAMA`,
`SKILLPP_LOCAL_MODEL`, `SKILLPP_EMBED_MODEL`. A test asserts no module outside
`config.py` contains the string `claude`, because otherwise the override is a
lie.

**No framework.** `skillpp/` is stdlib-only with no dependency manifest, which
is the point: it runs as a hook inside someone else's session and must never
break it. The Ollama call is one urllib POST. Google ADK was considered and is
the wrong trade here — agent orchestration, tool calling and session state for a
yes/no classification. Revisit only if the local side grows into multi-step work.

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

**The store on this machine holds one real promotion and five candidates
short of the threshold.** `bootstrapping-an-ephemeral-test-runner` sits at `x3`,
promoted 2026-08-20; the rest are at `x1` or `x2` and nothing expires them. The
funnel over its first two days: 18 sessions queued, 14 reviewed, 10 occurrences,
6 candidates, 1 promoted — an average of 1.67 sightings against a threshold of
3, so most candidates will never cross it. Whether that ratio is healthy is
unmeasured and is the open question behind everything else.

**The promoted skill exists only in `~/.claude/skills`, untracked.** It is the
pipeline's one durable output and it is in a home directory under no version
control. Three eval cases read it by path, so the tests are in git and the
thing they test is not. That is the team-distribution gap at its sharpest: the
handover says "skills are the team artifact" and nothing ships one anywhere.

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

## Fingerprint matching has no operating point — measured on their branch

Run against `feat/episode-filter` directly, its own model (`gemma3n:e4b`), its
own defaults, two real sessions from this machine. Their recognition step scores
a command-shaped signature (`bash:python3 | bash:ls`) and merges above
`SKILLPP_SIMILARITY`, shipped at **0.85**.

**Two ordinary sessions demand contradictory thresholds:**

| session | requirement | why |
| --- | --- | --- |
| three test-runner sessions | **< 0.460** | one procedure, three sightings, scoring 0.460–0.503 against each other. Above that they never merge, never reach ×3, never promote |
| a UI-work session | **> 0.703** | fifteen distinct tasks. Below that, *"I have figured out a new onboarding journey"* merges with *"my previous chat was interrupted"* at **0.703** |

`0.460 < 0.703`, so the window is **empty**. At their default the first session
splits one procedure into three; at a tuned 0.45 the second falsely merges three
unrelated tasks under a title that is a developer's prompt.

Command shape is not identity. Two Edit-and-preview loops on different features
score higher than two runs of the same procedure invoked slightly differently.

Compare the same decision by embedding (`similar.py`, measured on real
sessions): same procedure 0.848–0.873, different 0.513. A gap of ~0.3 against a
window here that is negative.

**Three claims to retract if they were repeated anywhere:** their pipeline does
*not* produce zero skills — tuned to 0.45 it drafted a good SKILL.md and
independently named it `bootstrapping-an-ephemeral-test-runner`, the same name
this branch chose. `similarity()` does not score 0.00 here; that number came
from reading a `signature:` key that is derived, not stored. And 0.45 does not
"work" — it was derived from the ledger it was then tested on.

### Taken from their draft, because it was better

- **Negative triggers.** Their body carries a *"Do not use this skill when…"*
  list — active virtualenv, project declares its own command, the request is to
  write a test rather than run one. Ours said only when it applied, and a skill
  that does that fires on the neighbouring case too. Now requested in
  `log-session.md`.
- **`requires_cli`.** `lifecycle.scan` and `skillpp check` have read this field
  since long before `promote-candidate` existed, and nothing ever wrote it — so
  every promoted skill claimed no dependencies and `check` could not fail. Now
  written, via `--requires-cli`.

### What their draft did not have

No mention of `PYTHONPATH`, from the same three transcripts. That rule exists in
our skill only because `verify` ran it and caught the collection error. It was
never in the sessions — it was found by *running* the skill, which is a thing
neither detector can do.

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
