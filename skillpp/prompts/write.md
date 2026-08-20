Below is a stretch of work between a developer and an AI agent.

Notation: `>` what the developer asked for · `$` a tool call · `=` it worked ·
`!` it failed · `.` what the agent said.

---
{SESSION}
---

Write instructions for a future agent that has to do this same job again.

Rules:

- Write **imperative instructions**, not a description of what happened. Say
  "run X", not "the agent ran X".
- Start with one line saying when to use it.
- Use `##` headings for each step or rule.
- Give the exact command where the exact command matters.
- Where something failed and was then fixed, write the **rule that avoids it**,
  not the story of it happening.
- Leave out anything belonging only to this one run: file paths specific to this
  repo, ticket numbers, names, dates, exact figures.
- No preamble, no summary, no closing remarks. Start at the first instruction.

Write the instructions and nothing else.
