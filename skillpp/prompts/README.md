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

`their-explore` is the remaining miss and it resists both wordings, stably
across three runs: eight greps, an edit, a successful commit, nothing failing.
The frontier definition counts a commit as confirmation; this model will not.
That is a floor rather than a bug, and it is left alone — the same fixture is
also the one the frontier judge over-records as a skill, so it is an awkward
segment by nature.
