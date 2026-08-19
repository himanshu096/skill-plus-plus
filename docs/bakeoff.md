# Two detectors, same fixtures

`feat/pattern-detection` (this branch) and `feat/ignore-list-and-drift-tracking`
solve the same problem from opposite ends, and both were built far enough to
test. This records what happened when the second one was run against the first
one's fixtures.

| | this branch | the capture branch |
| --- | --- | --- |
| When detection happens | at review, over a whole transcript | live, one hook payload at a time |
| What decides | a frontier model reading the session | span logic — prompt boundaries, closing-step regex, budgets |
| Cost per session | one model call | none |
| Runs unattended | no — someone types `/log-session` | yes |
| Needs an API key | yes | no |
| Shortest session it will look at | 25 new messages | any size |

The last three rows are the capture branch's case, and they are not small. What
follows is only about accuracy.

## How the comparison was made runnable

The two do not accept the same input. This branch takes a transcript path; that
one is never given a transcript at all — its candidates are whatever its span
logic banked from events during a live session. Handing it a fixture exercises
nothing.

`tests/evals/replay.py` closes that gap by replaying a fixture as the hook
stream it would have produced live: one `UserPromptSubmit` per developer
message, one `PostToolUse` per tool call with its result attached, a `Stop` per
assistant turn, a closing `SessionEnd`. It drives `skillpp hook` as a
subprocess, so neither branch has to import the other.

`tests/evals/bakeoff.py` runs every fixture through it and reports what was
banked. **Zero model calls** — which is itself the finding in the cost row
above: that branch's detection can be measured exhaustively for free, and this
branch's cannot.

## Results

Ground truth is each case's `asks` in `tests/evals/run.py`.

| Fixture | Should find | Banked at `max_span_prompts=3` (default) | At `=4` |
| --- | --- | --- | --- |
| `new`/`match` (a) | the bug-filing procedure | 1, wrong span — titled `"yes, link it"`, holding 4 unrelated greps and the two mutations; the method was abandoned | 1, **correct span** — 9 steps, all four MCP tools, leading greps trimmed |
| `match` (b) | the same procedure, ×2 | 1 ×1; both real spans abandoned (7 and 13 steps) | 1 ×1; second span still abandoned |
| `big` | one procedure in 433 KB | **0** — 14 spans abandoned | **0** — 13 spans abandoned |
| `incomplete` | nothing | 1 — titled `"ugh. leave it, I'll do it by hand tomorrow"` | **0** ✓ |
| `two` | two distinct procedures | 3 fragments | 2, but each only 2 steps |
| `secrets` | one, credential redacted | 1 ✓ | 1 ✓ |
| `barren` | nothing | 1 — `'run the tests'` | **2** — `"what's on this branch"`, `'what changed in those 3 files'` |

### One integer decides most of it

Changing `max_span_prompts` from 3 to 4 changed the verdict on four of seven
fixtures, **in both directions**: it fixed `new` and `incomplete`, made `barren`
worse, and left `big` and `match` broken at both settings.

That is the result worth keeping. No value of that integer gets all seven
right, because what separates a procedure from a morning of poking around is
not how many times the developer pressed enter. Three prompts is also the
natural shape of the workflow it was tested on — orient, request, confirm — so
the budget fires exactly one prompt before the mutation it was waiting for.

### `big` cannot be tuned at all

| Setting | Result |
| --- | --- |
| Budgets at default | 0 banked, every span abandoned |
| Every budget lifted | **1 candidate of 440 steps**, titled `'long one today — starting with the search index'` |

Nothing sits between them. The guard that stops a 440-step junk recipe is the
same guard that stops every real procedure in a long session, and a long
session is where a recurring procedure actually hides.

`docs/detection.md` on that branch already concedes the underlying point —
"Decide which 6 of 40 steps are the method → **No.** Needs the whole span in
working memory and reasoning about what superseded what." This is that
conclusion measured. The span logic is a proxy for a judgement, and the proxy
is load-bearing.

### Titles

Candidates are titled from a developer prompt, which produced `"yes, link it"`,
`"morning — starting on the support queue"`, `"what's on this branch"`, and
`"ugh. leave it, I'll do it by hand tomorrow"`. A skill's description is the
only thing read when deciding whether to load it, so a skill named after a
greeting never fires — the candidate can be technically correct and still dead.

## What was ruled out

**MCP blindness is not the cause.** `_is_closing_step` requires `tool == "Bash"`,
so the obvious hypothesis was that these MCP-heavy fixtures could never close a
span. Control: `recurrence-a` with both mutations rewritten as `gh issue edit`
and a `curl`, everything else identical. **Same outcome** — same 7-step span
abandoned, same wrong candidate. The abandon fires at the third prompt, before
any mutation exists to recognise, so no closing-step vocabulary could have
saved it. The narrow regex is a real limit for MCP-terminated work; it is not
what these results are about.

## The symmetric round: its own scenarios, its own turf

The fair objection to everything above is that the fixtures were written for
this branch's detector, so they decided the outcome. `tests/evals/their_scenarios.py`
removes it: six sessions built from the scenario table in that branch's own
`docs/detection.md`, deliberately on its terms — Bash rather than MCP, closing
steps its regexes actually match (`npm test`, `pytest`, `ruff`, `git commit`,
`helm upgrade`), every span inside its budgets, and `lgtm` where a task
boundary is wanted.

| Scenario | Its table expects | Banked |
| --- | --- | --- |
| `refine` — one procedure across two requests | 1 recipe, all steps | **2** — fragmented; `ruff check` + `git commit` became a recipe of its own |
| `retry` — failed on a lock, then passed | 1 recipe, folded after the pass | **2**, one of them at ×2 — a *false* match merging unrelated fragments |
| `distinct-tasks` — two tasks split by `lgtm` | 2 recipes | **3** |
| `explore-then-fix` — 8 greps then the fix | 1 recipe of 2 steps, greps trimmed | **1 of 2 steps** ✓ |
| `mid-investigation` — six reads, no conclusion | nothing banked | **0** ✓ |
| `recurs` — the same rollout twice | 1 recipe at ×2 | **3, all at ×1** |

Two of six, on its own examples. The fixtures were not the reason.

### Every banked recipe is two steps

That is the cause, and it is visible in the shape of the output. `Stop` runs
`fold_pending(require_success=True)` after every assistant turn, and
`looks_successful` is satisfied by two substantive steps ending in a closing
step. `npm test`, `ruff`, `git commit` and `helm upgrade` are all closing steps.
A real procedure passes several of those in a row, so it is folded at the first
one and the rest start a new span — the procedure is chopped at every gate it
passes.

Its own `docs/detection.md` names this exact risk while narrowing a different
regex: "reading `kubectl` alone as completion splits recipes exactly as
`git status` did." The narrowing was applied to `MUTATING`; the gates were left
in, and they split recipes the same way.

### Recurrence never worked

`recurs` is two runs of one procedure differing only in the service name and
image tag. Lexical dedup should be at its strongest here, and it banked three
separate entries at ×1. Meanwhile `retry` produced a ×2 by merging two
*unrelated* two-step fragments.

So the matching is simultaneously too weak to catch an identical procedure and
strong enough to merge things that have nothing to do with each other. A design
whose entire promotion gate is a recurrence count cannot reach that count, and
this is the same failure this branch measured on fingerprint matching before
abandoning it: `edit:.md | bash:cd` at 178 occurrences, `similarity()` at 0.00
on every real pair.

## Verdict

**This branch has the better detector.** The deciding results, in order:

1. **`recurs`.** A count-based promotion gate that cannot count an identical
   repeated procedure has no path to promoting anything.
2. **`big`.** Budgets on banks nothing from a long session; budgets off banks
   one 440-step blob. No setting between them, and a long session is where a
   recurring procedure hides.
3. **Fragmentation on its own scenarios.** Two of six, with the cause structural
   rather than tuned — folding at each passed gate is what `Stop` is for.

**What that branch wins, and it is not small:** no model call, no latency, no
API key, it runs unattended, and it will look at a session of any length. This
branch declines anything under 25 new messages, because a small increment is
not worth a model call — the messages are not lost, but they wait. On a real
session that rarely bites; on a short one, that branch covers ground this one
does not. This branch costs one frontier call per
session and only runs when a person types `/log-session`. The end state that
follows from both columns is that branch's trigger with this branch's judgement
— not a merge of two detectors.

## Two caveats, before quoting any of this

**~~The fixtures were written for this branch's detector.~~** Answered by the
symmetric round above: on six sessions built from that branch's own scenario
table, in its own vocabulary, it scored two of six. The bias was real and it
was not what decided the outcome.

**Only half of that branch has been measured.** Its design puts judgement at
review time: `/skillpp-review` reads the repo, resolves what it can, and asks
at most three questions. A bad span or a greeting for a title could be
salvaged there, and none of that has been run — it needs model calls. The one
result that survives regardless is `big`, where nothing was banked at all, so
review has nothing to look at. That failure is final.

## Still to run

- The paid half: `/skillpp-review` against the candidates banked above, to see
  how much of a bad span a review recovers.
- **This branch against the same six symmetric fixtures.** Added to
  `tests/evals/run.py` as `their-refine`, `their-retry`, `their-distinct`,
  `their-explore`, `their-mid`, `their-recurs`. This is the other half of the
  symmetric round and the only thing that could still overturn the verdict: if
  this branch also fragments `refine` or misses the recurrence in `recurs`, the
  gap is smaller than stated. Needs model calls.

  First attempt returned 0/6 and none of it was about detection: all six
  fixtures were built to that branch's span budgets, which put every one of them
  under this branch's 25-message floor, so `prepare-session` correctly answered
  "stop" and the model was never asked. The floor is now overridable
  (`SKILLPP_MIN_NEW`) and the six cases set it, since an eval is the one caller
  that means to pay the cost of a small session.
- This branch's original 13 cases as one sweep. Six were verified after the
  last prompt change; the other seven are older than the current command file.
