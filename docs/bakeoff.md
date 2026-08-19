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

## Two caveats, before quoting any of this

**The fixtures were written for this branch's detector, and tuned against it.**
A miss can be fixture bias as easily as a design limit. The control above is
what separates them for the MCP question; nothing similar has been run for
`two` or `barren`.

**Only half of that branch has been measured.** Its design puts judgement at
review time: `/skillpp-review` reads the repo, resolves what it can, and asks
at most three questions. A bad span or a greeting for a title could be
salvaged there, and none of that has been run — it needs model calls. The one
result that survives regardless is `big`, where nothing was banked at all, so
review has nothing to look at. That failure is final.

## Still to run

- The paid half: `/skillpp-review` against the candidates banked above, to see
  how much of a bad span a review recovers.
- This branch's own 13 cases as one sweep. Six were verified after the last
  prompt change; the other seven are older than the current command file.
- Recurrence on that branch. Every fixture banked ×1, so its 3× threshold was
  never reached by anything, and its matching was never exercised.
