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

## Known limits

- A procedure longer than the carry is intact in no window.
- A window can hold a procedure's tail without its method. At `BUDGET`, the
  third window of the `big` fixture holds `"yes, link it"` but neither the spec
  read nor the duplicate search. A caller that trusts each window on its own
  will propose halves.
- 19% of boundaries carry nothing, as above.

All three want the same thing: a reconcile pass over per-window findings.

## Still to measure

**Whether windowing loses procedures the un-windowed judge finds.** Take ten
real sessions, run the current un-windowed `/log-session` on each, treat what it
finds as the labels, then run the windowed version and compare. Both arms use
the same judge, so the difference is the windowing. That is the honest number
this document approximates at 90.3%, and it costs about twenty model calls.

Nothing here has been run against a model. `skillpp/window.py` has no caller
yet, so no eval was needed for any of it.
