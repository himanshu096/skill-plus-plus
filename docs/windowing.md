# Windowing: how much a split costs

> **How much of this is established.** Three tiers, and they have been reported
> in the same tone when they should not be.
>
> **Measured on data not built for the purpose:** the census — 14 of 14 real
> sessions exceed a 16k context after a 47x extract — and the segment-size
> distribution behind `BUDGET`. Those hold.
>
> **Weak:** every locator score. Twenty-one segments written *and* labelled here,
> with the prompt revised seven times against them; the four cold fixtures are
> the only unfitted evidence and they are six segments. Percentages computed
> from them, including "zero agreed-wrong" at n=3 disagreements, are
> illustrations rather than rates.
>
> **Not established at all:** that this pipeline detects the procedures the
> current whole-session prompt detects. There is no end-to-end measurement,
> because nothing calls `window.py` or `reconcile.py` yet. The direction being
> right does not make this implementation of it work, and the numbers below
> should not be read as saying it does.

A session does not fit a local model. Measured on twelve real transcripts, the
extract runs 60k–128k tokens *after* a 47× reduction, and every remaining
compression summed to about 5%. So the prompt is split instead — and the whole
risk of splitting is that half a procedure reads as a finished one, which is how
the capture branch failed.

This records what the split costs, and four attempts to make it cheaper that
all made it worse.

## Sizing, from measurement

Prompt-to-prompt segments across twelve sessions, 1,577 of them:

| | tokens |
| --- | --- |
| median | 290 |
| p90 | 1,458 |
| p95 | 2,248 |
| p99 | 4,507 |
| max single segment | 11,940 |

A procedure is a few segments, so three of them cost 4.4k at p90 and 6.7k at
p95. `BUDGET` is **8,000**: it clears that with headroom, leaves room for the
instructions and the store beside it at 16k, and only 3 of 1,577 segments exceed
it alone. Those get a window to themselves rather than being cut.

## The carry is tokens, not segments

Without overlap, a procedure lying across a boundary is in no window whole.
Carrying back a *count* of segments is the obvious fix and the wrong one: the
segments sitting at the end of a full window are the large ones.

| carry | windows | duplicated | segments carried |
| --- | --- | --- | --- |
| 2 segments | 163 | **48.1%** | 9.6 per window |
| 2,500 tokens | 155 | 27.1% | 11.4 |
| **1,500 tokens** | **134** | **12.0%** | **11.5** |
| 800 tokens | 125 | 4.0% | 11.1 |

A token budget does the same job for a quarter of the cost *and* covers more
segments, because it picks up a run of small ones instead of two big ones.
`OVERLAP` is 1,500 — p90 of a single segment.

## What it recalls

No ground truth exists for where procedures are in a real session, so this is a
structural estimate, not a measurement. Procedure length was estimated with the
capture branch's own closing-step vocabulary as a proxy; per-length recall is
computed from where the 155 real boundaries fall and how much each carries.

| length | share of procedures | recall |
| --- | --- | --- |
| 1 segment | 56.8% | 100.0% |
| 2 | 14.6% | 98.1% |
| 3 | 7.9% | 93.8% |
| 4 | 4.8% | 88.0% |
| 5 | 2.3% | 80.9% |
| 6 | 4.6% | 72.5% |
| >8 | 7.1% | — |

**Weighted: 90.3%.**

**Read it as an optimistic bound.** Three reasons:

- **The length proxy biases short.** It uses the closing-step regex that
  `bakeoff.md` shows firing on `npm test`, `ruff` *and* `git commit` in a row,
  which splits one procedure into pieces — hence a median of 1.0 segment. Real
  procedures are longer, so real recall is lower.
- **Placement is assumed uniform.** Procedures cluster; boundaries do not fall
  randomly with respect to them.
- **This is the windowing half only.** Total recall is windowing × judgement,
  and judgement is the larger term.

## Four ways of closing the 19% leak, all worse

19% of boundaries carry nothing at all. The carry must stay contiguous with the
boundary — reaching past a segment that will not fit, to take smaller ones
behind it, would start the next window with a request missing from its middle —
so a boundary whose last segment exceeds 1,500 tokens carries nothing. That is
the single largest known leak, and it looked like the obvious thing to fix.

| variant | boundaries | carry nothing | duplicated | recall |
| --- | --- | --- | --- | --- |
| **whole segments only (kept)** | **155** | 19% | **11.9%** | **90.3%** |
| truncated tail, fragment worth 0.5 | 173 | 0% | 29.6% | 89.4% |
| truncated tail, worth 1.0 (generous) | 173 | 0% | 29.6% | 90.0% |
| close before an oversized tail | 256 | 21% | 21.2% | 87.4% |
| both together | 279 | 10% | 38.8% | 86.6% |

Every mechanism that closes the leak **adds boundaries**, and boundaries are
what lose procedures. Carrying a fragment pushes tokens into the next window,
which brings the next boundary sooner. Closing early to keep an oversized
segment whole creates a boundary of its own — 256 against 155.

Even crediting a carried fragment as fully as a whole segment, it loses. The
leak is real; it is cheaper than any of its fixes.

**So: do not retry these.** If the 19% is worth closing, it needs a mechanism
that does not add a boundary — a merge over what the windows report, which is a
reconcile step rather than a carry.

## Known limits, and the pass that answers them

- A procedure longer than the carry is intact in no window.
- A window can hold a procedure's tail without its method. At `BUDGET`, the
  third window of the `big` fixture holds `"yes, link it"` but neither the spec
  read nor the duplicate search. A caller that trusts each window on its own
  will propose halves.
- 19% of boundaries carry nothing, as above.

All three arrive as the same symptom — a procedure reported in pieces — and
`skillpp/reconcile.py` is where the pieces are put back together. It has to
resolve two forces pulling opposite ways: the overlap deliberately shows one
procedure to two windows, so the same finding must not count twice, while a
boundary splits one procedure in two, so two halves must become one. Dedupe too
eagerly and a split collapses into a half; merge too eagerly and two procedures
become one entry, which is the silent failure — an inflated count and a
discarded proposal with nothing recording that it happened.

**Provenance decides, not prose.** Each finding carries the segment span it drew
on, and that separates most cases mechanically:

| relation between two findings | verdict | decided by |
| --- | --- | --- |
| overlapping spans, same name | duplicate | code |
| overlapping spans, different names | ambiguous | model |
| adjacent spans, different names | possible split | model |
| adjacent spans, same name | recurrence | code |
| disjoint spans, same name | recurrence | code |
| disjoint spans, different names | distinct | code |

The row worth staring at is *same name, disjoint spans*. That is a **recurrence**
— one procedure genuinely done twice, which is what `their-recurs` tests — and
folding those two into one because the names match destroys the evidence the
entry earns its place. Name matching alone gets this backwards.

Names are compared for equality after normalising case and separators, and
nothing softer. Lexical similarity was measured on this exact problem and
returned 0.00 on every real pair of names for the same procedure, because the
same work gets named differently every time. Anything below equality is a
judgement, and judgements go to a model rather than being guessed at.

**The judgement cost is bounded by ambiguity, not by window count.** Across the
twelve sessions there are 155 window boundaries, about 12 per session, which is
the ceiling on questions — and only boundaries where *both* adjacent windows
reported something can produce one. Most windows report nothing, because most
sessions contain nothing.

Verified against `big`'s real windows, `[(0,6), (6,15), (15,18)]`, using the
measured failure where window 2 holds the procedure's tail alone:

    both windows name it the same    -> 0 questions, 1 settled  (deduped in code)
    the tail is named differently    -> 1 question,  0 settled
    plus unrelated work elsewhere    -> 1 question,  1 settled

## Compression techniques, measured against this corpus

Two external sources were checked rather than taken on trust: a tool (`sqz`)
that caches repeated tool output and returns a short reference, and an article
listing five small-context techniques. Everything below is measured on the same
twelve sessions, 4.2 MB of extract.

Position matters for the first one. `sqz` sits between tool calls and the model
*during* a session; this reads a transcript afterwards, through an extractor
that already drops `Read` results outright and caps every result at 3 lines and
200 characters. Most of what it saves is already gone here.

| Claim / technique | Measured here | Verdict |
| --- | --- | --- |
| Repeated file reads | `Read` results are already dropped; repeated *calls* are 1.8% | mostly already free |
| Verbose JSON, null fields | **70 instances in 4.2 MB** | does not apply |
| Repeated log lines | 4.2% of the extract | real, see below |
| Consecutive repeats collapsed, count kept | **0.0%** — 36 runs in 12 sessions, longest 3 | dead end |
| Duplicate lines replaced with a back-reference | 3.3% results only, 6.6% all lines | available, unverified |
| Observation masking, keep 4 newest segments | 3.7% | available, contradicts a measured prior |
| Observation masking, mask every success | 8.2% | measured-bad — see `extract.py`, dropping results "lost almost every concrete claim" |
| Sliding window / FIFO truncation | — | **wrong for this problem.** A procedure is anywhere in the session; dropping the oldest turns drops procedures. |
| Token budgeting | already done — `BUDGET` | done |
| Rolling summaries | already done — the extractor, 47× | done |
| RAG instead of pasting the store | not measured; this is the local-embeddings step | separate work |

Two findings worth keeping.

**The duplication is scattered, not clustered.** 9.4% of extract lines are exact
repeats of an earlier line, but consecutive runs account for 0.0% of it. So a
retry loop is not what repeats — the same line recurs far apart in the session.
That kills the safe fix (collapse a run, state the count) and leaves only the
unsafe-looking one, a back-reference the model has to resolve by scanning.

**Failures are 0.5% of the corpus.** Keeping every failed result while masking
successes is therefore free, which makes graded masking cheaper to try than it
looks. What stops it being obvious is `extract.py`'s own measurement: dropping
results lost almost every concrete claim a summary made — which binaries were
missing, that the suite passed, what the coverage was.

**None of this enables a local model.** Stacked optimistically at 10%, the
corpus goes from 60k–128k tokens to 54k–115k, still three to seven times over a
16k context. These are frontier-cost savings, in the same category as the
heredoc cap. Windowing is the only measure that reached local range.

## Splitting by aspect, not only by position

Windowing cuts a session by position. It can also be cut by *aspect* — show a
first pass only the tool calls, and only escalate to the rest of a request where
that pass suspected something. The two compose: a first pass over 40% of each
segment fits four times as much session in a window.

Aspect shares across twelve real sessions (4.2 MB of extract):

| aspect | share |
| --- | --- |
| tool calls | 40.4% |
| agent narration | 31.7% |
| results, succeeded | 19.7% |
| developer prompts | 7.4% |
| results, failed | 0.8% |

What that buys, at the same 8,000-token budget:

| first pass shows | boundaries | tokens | locating recall |
| --- | --- | --- | --- |
| everything (today) | 155 | 1,180,469 | 88.0% |
| **tools only** | **58** | 492,610 | **96.9%** |
| tools + prompts | 70 | 576,693 | 95.9% |
| tools + prompts + narration | 124 | 940,260 | 90.3% |

**Recall rises from 88.0% to 96.9%**, and not because the judgement improved —
because there are 58 boundaries instead of 155, and boundaries are what lose a
procedure. It is the largest recall gain measured anywhere in this document, and
it comes from showing the model *less*.

Cost falls too, by 28% to 48% of a single full pass depending on how many
segments escalate. Escalating 53% of them — the share of labelled segments that
landed — still saves 28%.

### The part that decides whether it works

Tools are the cheapest aspect and may be the weakest evidence for the question
the first pass has to answer. Whether a request *resolved* is usually stated in
the agent's account — "green, committed" against "nothing conclusive yet, I'd
need a profile" — and that is the 31.7% a tools-only pass withholds. A command
list ending in a commit is a decent proxy; a command list is also what
`docs/bakeoff.md` shows a regex failing on, firing at every gate a procedure
passes.

So the ablation is the experiment, not the design: the same `/locate`
instructions over the same labelled segments, once with everything and once with
`--aspect tools`. `locate-tools-*` cases exist for the three fixtures where the
account should matter most — a change that was edited but never verified, an
investigation that concluded nothing, and two long segments of pure reading.

### What the first run said

Run on a frontier model, seven full-content cases and three paired against
`--aspect tools`.

| fixture | full content | tools only |
| --- | --- | --- |
| `refine` | wrong on segment 0 — and passed on a rerun, so flaky | ok |
| `mid-investigation` | ok | ok |
| `recurrence-a` | wrong on segment 1 | wrong on segments 0 and 1 |
| `retry`, `distinct`, `explore`, `recurs` | ok | not run |

Tools-only cost exactly one extra segment, and both its errors ran in the
conservative direction — a segment called unresolved when it was empty. Nothing
was called finished that was not.

**A third run broke that reading.** After the fixes below, full content went 2/2
and tools-only failed differently: segment 2 of `recurrence-a`, the segment where
the issue is updated and the thread posted back, came out `open`. That is the
destructive direction — an `open` verdict on finished work means nothing looks at
it again, while a wrong `landed` only costs a later judgement that finds nothing.
Two runs of conservative errors were not enough to claim the errors are
conservative.

Its cause was an instruction, not the model. The prompt said a command list
"that ends in a read" is usually not `landed`, and that segment's mutations sit
mid-list followed by four unrelated greps. Real segments end with unrelated work
constantly — a status check, the start of the next thing — so reading the final
line as the verdict marks finished procedures unresolved. The rule now asks
whether the work was *carried out* anywhere in the segment and says outright that
position is not evidence.

**And a command list alone has no success signal.** A mutation is visible; whether
it took is not, because results are withheld. Failures are 0.8% of a corpus, so
the cheap pass now shows tools *and* failures — 41.2% of a segment instead of
40.4% — and absence of a failure becomes the evidence that a change worked.

Two things the run exposed were faults in the question, not the answers.

**The three-verdict version could not be answered consistently.** Every
disagreement was `none` against `open` and none involved `landed`. Segment 1 of
`recurrence-a` — read the intake spec, search the tracker twice — is "nothing
happened" and "unresolved" simultaneously, because it is the first half of the
procedure that finishes in segment 2. Downstream both mean *do not start a span
here*, so the distinction was costing accuracy and buying nothing. Collapsed to
`landed` / `open`, which also fixed the label balance: it was 8/5/2, gameable by
never saying `open`, and is now 8/7.

**"The developer accepted it and moved on" was ambiguous.** In `refine`, the
developer's next message extends the same change — "now also cap it per tenant".
That is asking for more of it, not accepting it as done, and the flip between
runs was the model sitting on that fence. The rule now says acceptance looks like
a new subject.

Rescored under the collapsed labels, the answers already given would pass on
`recurrence-a` in both arms — tools-only matching full content exactly. That is a
rescoring rather than a result: the prompt changed, so it needs re-running.

### Confidence should come from agreement, not from asking

The natural way to combine staged passes is to ask each for a confidence and
multiply. Not worth doing: a model's self-reported confidence is a number, not a
probability, and combining several multiplies the error rather than reducing it.

Agreement is discrete and needs no calibration. Two passes over different
aspects that flag the same segment is evidence; two that disagree is a reason to
escalate that segment and nothing more. `reconcile.py` already resolves exactly
this shape of disagreement over spans, so the same machinery extends to aspects
without a confidence model.

## First local attempt, and why it failed

`tests/evals/local.py` asks a local model the same question over the same
fixtures, reconstructing the prompt *from the command file* — frontmatter
stripped, the `` !`command` `` line replaced with what that command actually
prints — because a hand-copied prompt would test the copy.

`qwen2.5:7b-ctx16k`, full content, four fixtures: **3 of 8 segments**, and the
reason is not judgement. Three of the four replies were byte-identical:

    0 landed
    1 open
    2 open

That is the example in the prompt's answer-format block, returned verbatim,
including three segments for a fixture that has one. The model recited the shape
instead of reading the session.

Two things worth keeping from that.

**A scorer that only checks labelled segments cannot see this.** `cold-bespoke`
has one segment labelled `landed`, so the recited answer's first line matched and
it scored 1/1 — a pass. The local scorer now returns zero for any answer naming a
segment that does not exist. `run.py`'s checker already did; this one did not, and
the first local run looked better than it was.

**The failure is the shape of the request, not the size of the model.** The
locator asks one question per *segment* and then asks for all the answers in a
single call. That is much closer to the five-questions-in-one-schema shape the
capture branch measured returning `task_count=40` than to the one-question-per-
call shape it measured getting three out of three in under three seconds. The
next thing to try is one call per segment: more calls, each roughly 200 tokens,
which is also the only version a 7B model was ever shown to handle.

Changing the prompt to fix this is not free, because the frontier arm is at 18 of
18 on it and would need re-verifying. Asking per segment instead leaves the
prompt alone.

**Memory, since it is a real constraint here.** Each model stays resident after
answering; two of these at once took an 18 GB machine to 16.5 GB. `local.py`
runs one model per invocation and unloads it when its sweep ends.

## One call per segment

The all-at-once prompt asks one question per segment and then wants every answer
in a single reply. That is nearer the five-questions-in-one-schema shape the
capture branch measured returning `task_count=40` than the one-question-per-call
shape it measured getting three of three in under three seconds.

`/locate-one` asks about one request and answers in one word. There is no
numbering in the answer, which removes the thing that was being copied.
`skillpp segments --only N` renders the single request; the prompt is 677 tokens
of instructions plus the segment, about 911 tokens on a typical one.

`qwen2.5:7b-ctx16k`, per segment, every fixture, full content:

| | all at once | one per segment |
| --- | --- | --- |
| segments right | 3 of 8 | **18 of 21** |
| distinct answers | no — three replies identical | yes |
| phantom segments | yes | none |
| time | 10s / 4 calls | 49s / 21 calls |

**The three misses are one miss, three times.** `their-refine` segment 0,
`recurrence-a` segment 0 and `cold-mixed` segment 0 all came back `landed`
against a label of `open`. Two of them contain a change that was never confirmed
— an edit with no gate, an edit benchmarked and reverted — and the third contains
no change at all, four greps. So the model is reading activity as completion:
under-applying the half of the `landed` rule that requires confirmation, and in
`recurrence-a`'s case the half that requires a change.

### The frontier control: 20 of 21, and a different miss

Run on identical text. It missed `recurrence-a` **segment 2** — `open` against a
label of `landed` — which the all-at-once prompt gets right. In isolation that
segment is `yes, link it`, two MCP mutations, four unrelated greps. The mutations
were carried out; nothing visible confirms them, because the confirmation in the
label is *the developer moved on*, and one segment alone cannot see that there
was a next request.

So the two arms fail differently, and both diagnoses are real:

| | miss | direction | cause |
| --- | --- | --- | --- |
| frontier | `recurrence-a` seg 2 | `landed` → `open` | the prompt cannot see what came next |
| local 7B | segment 0 of three fixtures | `open` → `landed` | reads activity as completion |

### Showing the next request made the local arm worse

`--only N` now prints the following request's opening line beneath the segment,
as context, with the prompt saying a new subject corroborates `landed` *only if
something was carried out*, more of the same means `open`, and nothing following
is weak evidence either way.

Local went **18 of 21 to 16 of 21**. Three segments that had been right broke,
each by taking the peek as sufficient: a bare `lgtm` followed by a new subject
became `landed` though nothing happened in it, a finished request with nothing
after it became `open`, and a request followed by more of the same became
`landed`.

That is the fifth time an auxiliary hint in this prompt has been read as a rule,
and the first time it happened to a small model rather than a frontier one. The
pattern is sharper than "hints get promoted": **the hints a frontier model needs
are the ones a 7B over-applies.** Every revision that raised the frontier score
added nuance, and nuance is what the small model cannot weigh.

### The peek fixed the frontier arm, so there are two prompts

The frontier control with the peek came back **21 of 21** — `recurrence-a`
segment 2 flipped to `landed`, which is what the peek was written for. So the
same addition takes the judge from 20/21 to 21/21 and the 7B from 18/21 to
16/21. That settles the question: **two prompts, measured rather than assumed.**

`skillpp/prompts/` holds the local pair. They ask one visible fact at a time and
`landed` is `changed && worked`, combined in code rather than by the model —
which is the shape the capture branch measured a 7B handling three for three in
under three seconds, against the same model returning `task_count=40` when asked
five things at once.

| asking a 7B | right | time |
| --- | --- | --- |
| every verdict in one call | 3 of 8 — recited the example | 10s |
| one verdict per call | 18 of 21 | 49s |
| one per call, plus the next request | 16 of 21 | 61s |
| **two questions per request, combined in code** | **20 of 21** | **31s** |

Faster despite twice the calls, because each prompt is a few hundred tokens
instead of nine hundred.

So the cheap pass runs locally at 95% of the labelled segments, free, in half a
minute — against a frontier judge at 21 of 21.

### Which local model, measured

| model | segments | time | note |
| --- | --- | --- | --- |
| **qwen2.5:7b** | **20 of 21** | 31s | strict — refuses a change nothing confirmed |
| granite3.3:8b | 19 of 21 | 43s | lenient — accepts absence of failure |
| mistral:7b | 18 of 21 | 39s | lenient, shares granite's failures |
| llama3.1:8b | 16 of 21 | 37s | |
| llama3.2:3b | 15 of 21 | 15s | 3B is not enough |
| qwen3:4b | 2 of 5 | 99s | reasons past the budget; with thinking off it writes prose instead of one word |
| gemma4:12b | 0 of 21 | 228s | same, and 5x slower |

Size is not the constraint — whether the model will answer in one word is.
`qwen3:4b` is newer and larger than `llama3.2:3b` and does far worse, purely on
format. Anything reasoning-tuned is the wrong tool for this step.

### Combining two models beats fixing one

`qwen2.5:7b` and `granite3.3:8b` fail on *different* segments, because one is
strict about confirmation and the other is not.

| combiner | agreed and right | agreed and **wrong** | escalated |
| --- | --- | --- | --- |
| **qwen2.5 + granite3.3, unanimous or escalate** | 18 | 0 | 3 (14%) |
| qwen2.5 + mistral | 17 | 0 | 4 (19%) |
| granite3.3 + mistral | 20 | **2** | 1 (5%) |
| all three | 17 | 0 | 4 (19%) |
| all three, majority vote | 19 of 21 | — | — |

Two things worth keeping. **Majority voting is worse than the best single
model** — two of the three agree wrongly on the marginal segments and outvote
the one that had them right. And **`granite3.3 + mistral` looks best on
escalation rate and is the worst option available**: they share two failure
modes, so they agree confidently and wrongly twice, which is silent.

The pair to use is a strict model with a lenient one, never two of the same
temperament. Their disagreement is computed in code, needs no calibration, and
lands on the segments where the judgement is marginal — which is what a
confidence score is supposed to provide and does not.

With the caveat that matters: this is three disagreements across twenty-one
segments of my own construction. It is a mechanism that looks sound, not a
measured error rate, and the number to trust instead is the end-to-end one
below, which has not been run.

### Keeping the two prompts honest

`landed` is defined identically in both command files, because a command file
cannot include another. `TestTheTwoLocatePrompts` asserts the block is the same
text, that both still require both halves, and that the distributable copies in
`commands/` match the active ones in `.claude/commands/` — `install.py` copies
from the former, so a fix applied only to the latter ships the old prompt. It
also asserts the single-segment prompt contains no numbered example, since that
is what was recited.

## Three real sessions: the cheap pass cannot filter

| session | requests | tokens | spans | reduction | single-request spans |
| --- | --- | --- | --- | --- | --- |
| concept-buddy, small | 13 | 23,967 | 7 | 4% | 5 of 7 |
| concept-buddy, medium | 40 | 60,180 | 22 | 0% | 13 of 22 |
| skill-plus, large | 173 | 83,699 | 32 | 0% | 12 of 32 |

**The 0% is arithmetic, not tuning.** `spans()` partitions the session: every
stretch between two landings is emitted, and a partition cannot reduce anything.
The only reduction that appears anywhere is trailing work still open when the
session ended — that is the 4%.

So the error is deeper than where the boundaries fall. For a cheap pass to
filter it has to be able to **discard**, and this one cannot: every commit is a
landing, every landing makes a span, so it can never output nothing. Yet the
premise of the whole project is that most sessions contain nothing worth
keeping.

**The decomposition into visible facts cannot produce the filter, because the
filter needs the judgement the decomposition exists to avoid.** "Did work land"
is mechanical and a 7B answers it at 20 of 21 on short inputs. "Is there a
procedure here worth capturing" is the judgement, and that is the thing this was
meant to make cheap.

Two directions that would not have this defect, neither measured:

- **Rank instead of filter.** Hand the judge spans in order of promise and let
  it read until a token budget runs out. A cheap pass that cannot discard can
  still order, and ordering is worth something when the budget binds.
- **Filter on a different visible fact.** Whether a span touched anything
  outside the machine — a ticket, a deployment, a message, a published package —
  is mechanical, and it is closer to "procedure worth repeating" than "work
  landed" is. It would discard the `commit as-is` spans, which is most of the
  noise here.

### The first of the three, in detail

Run end to end on a real 40-request session, 60,180 tokens, after the context
bug above was fixed.

    22 spans, covering all 40 requests, 60,180 tokens
    reduction in what a judge must read: 0%
    contiguous partition of the whole session: True
    13 of 22 spans are a single request

**The cheap pass filters nothing.** The spans tile the session end to end, so
every token still reaches a judge — split across 22 calls instead of one, which
is worse than not doing it: more calls, and the judge cannot see across a
boundary.

The verdicts are not the problem. It marked `commit as-is`, `push it` and
`commit + push as-is` as landed, and each of those did land work. The problem is
`spans()`, which treats every landing as a procedure boundary. A developer
saying "commit as-is" lands work belonging to the *previous* procedure, so the
real procedure gets cut across four consecutive spans.

That is the failure `docs/bakeoff.md` measures the capture branch having — a
recipe chopped at every gate it passes — reproduced here at a different gate.
Being right about "did work land" turns out not to answer "where does a
procedure end", and the whole design assumed those were the same question.

Two things this says about the work above. The locator scores are not wrong but
they are answering a question that does not compose into the thing it was built
for. And the toy fixtures could not have shown this: each holds one procedure
with no follow-up "commit it" request after it, so a landing and a boundary
always coincided.

What would fix it is not another verdict. A boundary needs a landing *and* the
next request starting a new subject — which is the peek that
`skillpp segments --only N` already prints and the frontier prompt already uses.
Unmeasured, and not built, because the last five things built here were built
before the measurement that would have shaped them.

## Still to measure

**Whether windowing loses procedures the un-windowed judge finds.** Take ten
real sessions, run the current un-windowed `/log-session` on each, treat what it
finds as the labels, then run the windowed version and compare. Both arms use
the same judge, so the difference is the windowing. That is the honest number
this document approximates at 90.3%, and it costs about twenty model calls.

Nothing here has been run against a model. `skillpp/window.py` has no caller
yet, so no eval was needed for any of it.
