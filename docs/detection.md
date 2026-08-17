# Detection: what decides a span is a recipe

Capture has to answer three questions about a stretch of work. They look alike
and are not:

| Question | Difficulty | Where it is answered |
| --- | --- | --- |
| **Where does the task end?** | Mechanical | Regex — closing steps (`_is_closing_step`) |
| **Where does it begin?** | Structural | Prompt boundaries + leading-exploration trim |
| **Which steps are the method?** | Judgement | Deferred to review time |

The third is the hard one, and most of this document is about why it does not
belong in the capture layer.

---

## The regex trap

Every detection bug found on 2026-08-17 had the same shape: **a regex written to
answer one question, reused to answer another.**

| Reused | Written for | Broke because |
| --- | --- | --- |
| `signals.VERIFYING` | "does a deploy have a check after it?" | matches `status`, `diff`, `ps`, `get` — mid-task orientation commands |
| `signals.MUTATING` | "does this reach outside the machine?" | matches bare `kubectl`, `terraform` — so `kubectl get pods` read as completion |
| `_SUCCESS_RE` + length guard | "is this a confirmation?" | `"that works? I doubt it"` passed; `"perfect, now add a cap"` swallowed the request |
| `_FAILURE_RE` | "is failure handling described?" | matched the phrase *"no failure handling"* as evidence *of* failure handling |

Each was fixed by narrowing. Each time a new one appeared, because narrowing a
pattern does not fix a category error: these questions need to know what role a
word plays in a sentence, and a regex cannot.

**The rule that came out of it:** detection regexes are single-purpose. If a
pattern in `signals` looks useful in `capture`, it is answering a different
question — write a new one and say so in a comment. `_CLOSING_MUTATE_RE` exists
precisely because `MUTATING` could not be borrowed.

---

## Can a local model do the judging?

The recurring proposal is to stop guessing with regexes and ask a model whether
a task is finished. Tested against local models through **Ollama** — local
because a hook that needs an API key, a network round-trip and a per-turn cost
is not a hook anyone will keep installed.

Tested 2026-08-17 against a real 53-step candidate. Ground truth: **two distinct
tasks, verdict `split`.**

### First attempt — five questions at once, raw commands

| Model | Time | Result |
| --- | --- | --- |
| `gemma3n:e4b` | 16s | `task_count=40` (counted *steps* as tasks), kept **37 of 40** steps |
| `qwen3.5:9b` | — | no valid JSON, despite a schema |
| `gemma4:12b` | 77s | `task_count=3`, kept **6 of 40** — real judgement, but slow and still said `promote` |

E4B's answer was not merely wrong, it was wrong at the one thing the filter
exists to do: it kept almost everything.

### Second attempt — one question at a time, cleaned input

Same model, same span, `crisp()`-style cleaning applied first:

| Question | `gemma3n:e4b` | Truth | Time |
| --- | --- | --- | --- |
| tests were run? | `True` | True ✓ | 2.7s |
| committed? | `False` | False ✓ | 0.8s |
| name it | *"Skill development, testing, promotion"* | fair ✓ | 1.2s |

**Three for three, in under three seconds.** The first probe was a bad
experiment, not a bad model.

### What actually changed

- **Input cleaning did most of the work.** Dropping `echo` banners, pipes,
  redirects and heredoc bodies took 53 steps to 41 readable lines.
- **One question per call did the rest.** Five outputs in one schema is where
  `task_count=40` came from.
- **Over-cleaning breaks it too.** An intermediate attempt ran
  `normalize_command()` first, reducing `python3 -m unittest discover` to
  `python3` — and E4B then correctly said "not completed", because nothing in
  its input said tests had run. **Crisp is not the same as lossy.**

### Conclusion

| Task | Small local model |
| --- | --- |
| Extract a visible fact — tests run, committed, tool used | **Yes.** Sub-3s, free, private, no key |
| Name a task | Yes, roughly |
| Count distinct tasks | Unnecessary — prompt boundaries answer it exactly |
| **Decide which 6 of 40 steps are the method** | **No.** Needs the whole span in working memory and reasoning about what superseded what |

So the split is: **a local model extracts facts; judgement waits for review**,
where a frontier model is already in the session and costs nothing extra.

### Why not in the hook

Not a tuning question — disqualifying. Hooks run synchronously in the
developer's session, must exit fast, and must never fail. A model call adds
latency to every turn, needs a key, costs money per turn, and breaks the
fail-safe property that keeps capture from disrupting work. Any judging runs
deferred or at review, never inline.

---

## What was built instead

The structural fixes remove most of the need for a judge:

**Span budget.** A span crossing `max_span_prompts` (3) requests or
`max_span_steps` (40) steps without ever reaching a closing step is abandoned,
not banked. Previously it grew until the session ended — the shape that produced
one 216-step "recipe" covering several unrelated questions.

**Leading-exploration trim.** A span ends at a closing step but had no start
marker, so it swallowed the looking-around before it. A recipe's first real
action *changes* something; reads before that are how the problem was located.
Only a leading run is cut — `check the logs, then restart` is a real two-step
recipe.

**Exploration-only guard.** A span whose every step is read-only contains no
method, whatever path it arrived by. This replaced an earlier attempt requiring
a closing step at session end, which discarded a completed rollback that used a
bespoke script no regex recognised.

**`crisp()` and narration filtering.** Banner `echo`s never enter the buffer;
plumbing is stripped for display. Explicitly *not* `normalize_command()`, for
the reason above.

### Two principles worth keeping

1. **Length is not the failure; never closing is.** A finished 50-step migration
   is one recipe and is kept however long it ran. Only unfinished sprawl is
   abandoned.
2. **"Did anything happen" beats "did it close."** The latter is stricter in the
   wrong direction and throws away real work.

---

## Scenario coverage

Five end-to-end scenarios, run against the capture pipeline directly:

| Scenario | Expected | Status |
| --- | --- | --- |
| Refinement across two prompts | one recipe, all steps | ✅ |
| Failed, retried, passed | one recipe, folded after it passed | ✅ |
| Two distinct tasks back to back | two separate recipes | ✅ |
| Exploration (8 greps) then the fix | the 2 real steps | ✅ was 10 steps |
| Session ends mid-investigation | nothing banked | ✅ was 2 greps banked |

The last two failed before the work above; the fifth was worse than useless,
turning every abandoned investigation into a candidate.

---

## Still open

Whether any of this makes passive capture worth having. The mechanism is now
defensible; that is not the same as demonstrated. The ledger was cleared on
2026-08-17 so the question could be asked honestly:

**Did anything captured on its own become a skill that was kept?**

If the answer is no after a fortnight, the honest product is smaller — explicit
capture, dictation, and the lifecycle tooling — and worth having anyway.
