# Windowing: how much a split costs

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

If tools-only holds up, the first pass is 40% of the content at 96.9% recall. If
it collapses, the first pass has to include narration, which is 79% of the
content at 90.3% — most of the gain gone, and the honest conclusion is that this
axis mainly saves frontier cost rather than enabling a local model.

### Confidence should come from agreement, not from asking

The natural way to combine staged passes is to ask each for a confidence and
multiply. Not worth doing: a model's self-reported confidence is a number, not a
probability, and combining several multiplies the error rather than reducing it.

Agreement is discrete and needs no calibration. Two passes over different
aspects that flag the same segment is evidence; two that disagree is a reason to
escalate that segment and nothing more. `reconcile.py` already resolves exactly
this shape of disagreement over spans, so the same machinery extends to aspects
without a confidence model.

## Still to measure

**Whether windowing loses procedures the un-windowed judge finds.** Take ten
real sessions, run the current un-windowed `/log-session` on each, treat what it
finds as the labels, then run the windowed version and compare. Both arms use
the same judge, so the difference is the windowing. That is the honest number
this document approximates at 90.3%, and it costs about twenty model calls.

Nothing here has been run against a model. `skillpp/window.py` has no caller
yet, so no eval was needed for any of it.
