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
