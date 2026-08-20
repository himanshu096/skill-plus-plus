# Prompt templates

Prompts for a **local** model, kept here rather than in `.claude/commands/`
because nobody types them: they are library text a module fills in and sends,
not slash commands.

Why they exist separately from `/locate-one`, which asks the same thing in one
call: the two audiences need different prompts, and that was measured rather
than assumed.

`/locate-one` reached 21 of 21 segments on a frontier model and 16 of 21 on
`qwen2.5:7b`. Every revision that raised the frontier score added nuance —
what corroborates a verdict, how to weigh what the developer said next — and
each piece of nuance is something the small model read as a rule on its own. A
bare `lgtm` followed by a new subject came back as finished work because the
prompt said a new subject corroborates it.

So these ask one question at a time, about a visible fact, with no corroboration
lists and no context beyond the request itself:

| file | question | answers |
| --- | --- | --- |
| `changed.md` | did anything get changed? | `yes` / `no` |
| `settled.md` | it was changed — did it work? | `yes` / `no` |

`landed` is `changed && worked`, combined in code. That split is the shape the
capture branch measured a 7B handling well — three visible facts, three for
three, under three seconds each — against the same model returning
`task_count=40` when asked five things in one schema.

`{SEGMENT}` is replaced with the rendered request. Keep these short: length is
what invites a small model to find a rule in the wording.

## The scores below were measured on toy segments

Stated first because it changes how to read everything after it. The 21 labelled
segments are **102 tokens at the median and 225 at the largest**. Real segments,
measured across twelve sessions, are **869 at the median, 3,670 at p90 and
11,940 at the largest** — nine times bigger in the middle and fifty times at the
tail.

So these scores say a model can answer this question about a short request. They
do not say it can answer it about a real one, and the first real session run
found a defect the fixtures could not: five of its forty segments exceeded
Ollama's default 4096 context once the prompt was added, and Ollama truncates
from the front, where the instructions are. Those five were answered from a
mangled prompt with nothing reporting it. `num_ctx` is now sized per call.

The honest next measurement is real segments, which have no labels — so it is a
plausibility check against what the frontier judge finds on the same session,
not an accuracy score.

## Measured

`qwen2.5:7b-ctx16k`, 21 labelled segments across 11 fixtures, full content:

| asking | right | time |
| --- | --- | --- |
| every verdict in one call (`/locate`) | 3 of 8 — recited the prompt's own example | 10s |
| one verdict per call (`/locate-one`) | 18 of 21 | 49s |
| the same, plus the next request as context | 16 of 21 | 61s |
| **two questions per request, combined in code** | **20 of 21** | **31s** |

Faster than the one-call version despite twice the calls, because each prompt is
a few hundred tokens instead of nine hundred.

Two revisions were needed and both were the same mistake in the wording, not in
the model. `settled.md` said to answer `no` if the agent was "still looking",
and reads were taken as looking — any segment containing a grep failed. It now
says to ignore reading entirely. It also had to say **not** to require a test:
every segment the model accepted had a test or a health check in it, and it
refused a change that was only committed, which is a stricter rule than the
prompt asked for.

`their-explore` is the remaining miss. Located exactly, by ablation: the model
answers `changed` correctly and `settled` wrongly, and what it wants is an
**affirmative** signal that the change worked. Rewording the agent's narration
to say "Fixed it" flips it to correct; adding a passing test flips it. Deleting
the narration does not, and neither does cutting the greps from eight to two —
so it is not misled by anything present, it is missing something absent.

Three attempts to close it, none of which worked:

1. The prompt says outright "do not require a test… still gets `yes` if nothing
   says it failed". Ignored.
2. A fully deterministic rule — changed, and nothing failed, and nothing
   reverted — scores **17 of 21** against the labels, worse than the model. It
   reads `kubectl logs` as a write and cannot tell
   `mcp__confluence__get_page` from `mcp__tracker__update_issue`, which is the
   same wall `docs/bakeoff.md` measures the other branch hitting. It also calls
   `their-retry` unfinished, because a failure *inside* finished work looks the
   same to a regex as one that ended it.
3. Injecting only what code knows exactly — how many steps failed, whether
   anything was reverted — as a line beneath the segment. Byte-identical
   answers with and without.

So it is a floor. The fix is not in this prompt: `granite3.3:8b` gets this
segment right *because* it accepts absence of failure, and is correspondingly
looser where qwen is correctly strict. Running both and escalating where they
disagree left zero wrong answers on the segments they agreed about, at three
escalations in twenty-one — and the disagreement is computed in code, which is
the deterministic part that actually works.


## Triage: which sessions are worth a frontier call

`triage.md` asks one question about a whole session — did it carry out a
procedure worth following again — and it exists because the span pass answered a
harder question correctly and saved nothing. Of the last ten sessions drained,
five stopped free at the message floor and five spent a frontier call to find
nothing. A session-level decision would have saved all five.

The input is the developer's **requests only**, which is 7.4% of an extract.
That matters for one reason: the largest reviewed session is 127,485 tokens and
its requests are 4,764, so triage can read a session no judge-sized prompt would
fit. For the other eleven the whole session fits anyway.

Scored on `tests/evals/truth.py` — twelve sessions the pipeline has already
reviewed, labelled by what the frontier judge proposed. Real sessions, real
labels.

| | `qwen2.5:7b`, requests only |
| --- | --- |
| decided free in code (no request to read) | 3 |
| asked the model | 9 |
| sessions with a procedure, sent on | **6 of 6** |
| sessions without, correctly skipped | **1 of 3** |
| **procedures lost** | **0** |
| frontier calls avoided | 4 of 6 |
| time | 29s, free |

**Read the 1-of-3 rather than the 10-of-12.** The headline counts three
sessions that rendered to zero request tokens and were therefore answered
correctly without the model discriminating anything; those are now gated in code
by `worth_reading` and reported separately. What the model actually did was
separate one empty session from three, and lose nothing. That is the right error
direction and thin evidence — n=3 on the side that matters.

One revision was needed, and it was the same mistake as everywhere else in this
file. The prompt said "when you are unsure, answer `yes`", meaning to bias
toward recall. The model answered `yes` to all twelve sessions, which is the
same as not answering. Replaced with a statement that most sessions are `no` and
that answering `yes` to everything is not an answer.


## Writing the body: not possible on a 7B

The goal was no frontier model anywhere. It fails at exactly one step, and it is
the step that produces the product.

**One call.** `write.md` asked qwen2.5:7b for a body from a whole session. It
transcribed the run instead of generalising it, named the repository and the
function that run happened to touch, selected a procedure the frontier judge had
explicitly rejected as mechanical, and invented a command the session never ran.

**Four narrow calls.** The technique that took the locator from 3 of 8 to 20 of
21 — one question per call, assembled in code — applied here as `keep.md`
(is this step method?), `generalise.md` (strip this run's details),
`rule.md` (what avoids this failure), `trigger.md` (when to use it), with the
body assembled by `compose()` rather than written by the model. Each question is
narrow and each answer is short. It still fails:

- `trigger.md` describes the steps rather than the situation — "count its lines,
  extract the first entry's timestamp, enumerate through each line"
- `generalise.md` handles `git commit -am '<message>'` and fails on anything
  real, leaving an absolute transcript path in three separate steps and
  flattening heredocs into one unusable line
- `keep.md` reduced a docstring-and-test segment to `Read <file path>`, dropping
  the edit that was the point

The placeholders do work in isolation, which is the one encouraging part and not
enough. Selecting which steps are the method, and saying what situation calls
for them, both need the whole span held at once — which is what that branch's
`docs/detection.md` concluded about a small model and what this now confirms from
the other direction.

**Where that leaves it.** Everything except the body can be local: triage is
measured and wired, and matching is semantic similarity, which is an embedding
model's job rather than a generative one. The body needs a frontier model, or
hardware that holds something much larger than 12B. This is kept in the tree
rather than deleted so the next person does not spend the day rediscovering it.
