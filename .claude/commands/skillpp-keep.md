---
description: Save the current task as a skill candidate, without waiting for it to recur
---

# Skill Plus Plus — keep this task

The developer wants to keep the work that just happened as a skill candidate,
instead of waiting for the 3-occurrence threshold. Everything after the command
name is an optional note.

## 1. Capture the open span

```bash
skillpp keep --json
```

This folds the current task (steps since the last successful fold) into the
ledger and marks it ready immediately — same rule as `/skillpp-new`: an
explicit request is not noise.

If the result is `no-session` or `too-thin`, say so and stop. Do not invent
steps.

## 2. Show them what was kept

```bash
skillpp show <id>
```

Lead with the effect summary (commands, writes, destructive steps, network).
If they gave a note, treat it as the intended trigger.

## 3. Ask whether to write the skill now

One question: promote this into a `SKILL.md` now, or leave it in the ledger?

If they want the skill now, follow `/skillpp-review` from "Write the skill"
onwards (`scaffold` → edit → `promote`). If they want to leave it, stop.
