# Skill Plus Plus on Claude Code

How the detection loop actually runs: what fires during a session, what you
type, and what ends up on disk.

Rewritten 2026-08-24, when the ledger half was deleted. The version before this
described that half exclusively — three hooks, a capture pipeline and a review
command that had all stopped being reachable — and asserted in a table that the
hooks were "confirmed working against live payloads" when none were installed.
Everything below was checked against the code as it stands.

> **Invoke it as `python3 bin/skillpp`.** There is no `pyproject.toml`, so
> `skillpp` is not on `PATH`.

---

## 1. What happens during a session

Nothing. That is the design.

The only hook is `SessionEnd`, and all it does is append one line naming the
transcript to a queue. No prompt is read while you work, no tool call is
recorded, nothing decides anything mid-session.

Detection happens later, over the transcript Claude Code already wrote —
`skillpp drain`, or `/log-session` by hand. An earlier design captured every
prompt and tool call live into a second store; the review path never read any
of it.

```
session ends ─▶ SessionEnd hook ─▶ ended-sessions.jsonl
                                          │
                        skillpp drain ────┘   reads the transcript,
                                              spends one model call,
                                              records what it found
```

---

## 2. Install

```bash
python3 bin/skillpp install            # dry run: prints the plan, writes nothing
python3 bin/skillpp install --apply    # writes, after backing up settings.json
```

One hook is installed:

| event | what it does |
| --- | --- |
| `SessionEnd` | appends the ended session to its project's queue |

A session whose transcript is missing or unreadable is skipped rather than
queued — `claude -p --no-session-persistence` ends a session and fires the hook
but writes no transcript, so without that check every automated run left a dead
row.

### Checking it worked

```bash
python3 -c "import json,pathlib; d=json.loads(pathlib.Path.home().joinpath('.claude/settings.json').read_text()); print(json.dumps(d.get('hooks', {}), indent=2))"
```

`.get("hooks", {})` rather than `["hooks"]`: a settings file with no hooks key
is normal, and indexing it raises `KeyError` at the one moment you are trying
to find out whether anything is wired.

Then end a session and check the queue grew:

```bash
wc -l .claude/skillpp/memory/ended-sessions.jsonl
```

---

## 3. Where everything lives

The **store** is global; the **queue** is per project. A queued session names a
transcript belonging to one repository, and draining is something you do in
that repository.

```
~/.claude/skillpp/
  patterns/<name>.md          one recorded procedure, written once, never reopened
  occurrences.jsonl           one line per sighting — the count is how many there are
  decisions.jsonl             one line per human decision; the last one is the status
  exemplars.jsonl             embeddings of past sessions, for recognising a repeat
  reviews/<session>.md        what a session proposed, and how far it was read
  reviews/<session>.steps.jsonl   what that session literally ran, scrubbed
  drafts/<name>/SKILL.md      a redrafted body awaiting review; never installed

<project>/.claude/skillpp/memory/
  ended-sessions.jsonl        the queue `drain` reads

~/.claude/skills/<name>/SKILL.md    a promoted skill — the only thing an agent loads
```

Everything countable is derived. The count is the number of logged sightings,
the dates are their range, the status is the last decision — so no two records
of the same fact can drift apart.

---

## 4. Daily use

### Review the sessions that ended while nobody was looking

```bash
python3 bin/skillpp drain              # dry run: what it would spend
python3 bin/skillpp drain --apply      # one model call per session
```

Sessions below the review floor of **25 new messages** are skipped for free,
without a model call. Already-reviewed sessions are skipped by their bookmark.

### Review one session by hand

```
/log-session                    # this session
/log-session <transcript path>  # a specific one
```

Proposes procedures worth a skill, and records them. Most sessions contain
nothing, which is the common and correct answer.

### Decide on what is waiting

```
/review-candidates
```

Lists everything past the threshold, asks about one, and promotes it if you say
so. A candidate needs **3 sightings** before it is offered — below that it is a
log entry, not a question.

### See the store

```bash
python3 bin/skillpp candidates          # everything, grouped
python3 bin/skillpp search <words>      # by name and body
```

```
▸ x4  kept-procedure          waiting on /review-candidates
  x1  young-one               still logging
✓ x3  promoted-one            → ~/.claude/skills/…
✗ x9  turned-down             turned down at 4x · done 5x since
```

### Describe a skill instead of doing one

```
/dictate-skill <what it should do>
```

Asks what the description leaves out, then records it. It skips the recurrence
threshold: three sightings are a proxy for "worth someone's attention", and
describing a procedure on purpose supplies that judgement directly.

---

## 5. Deciding about a candidate

| | |
| --- | --- |
| **promote** | `/review-candidates`, or `promote-candidate <name>` |
| **turn down** | `reject-candidate <name>` — leaves the queue for good |
| **undo that** | `reopen-candidate <name>` |

A rejection is not a deletion. The entry, its body and its provenance stay, and
sightings keep accruing — but it is **never re-proposed**. Re-asking about
something you just refused is the fastest way to get the whole tool switched
off, and a count is not an argument you have not already heard. What the
continued counting buys is evidence you can look at: `turned down at 4x · done
9x since`.

### When a body turns out wrong

```bash
python3 bin/skillpp redraft <name>            # dry run
python3 bin/skillpp redraft <name> --apply    # one model call
```

Rewrites a body against the trace of what its sessions actually ran, and writes
a **draft** — never the store, never the skills directory. A skill's text
changing under you without review is the thing this avoids.

It refuses on an entry with no trace: dictated procedures were never observed,
and entries recorded before traces were kept have nothing to rewrite against.

---

## 6. Getting skills somewhere else

A promoted skill is a directory under `~/.claude/skills/`. Claude Code loads it
automatically in every project — `description` and `when_to_use` are read at
startup, before the body, and decide whether it ever fires.

```bash
python3 bin/skillpp bundle --out ./dist --format upload   # one zip per skill
python3 bin/skillpp bundle --out ./dist --format plugin   # .claude-plugin/ + skills/
```

`upload` is for Claude Desktop → Customize → Skills. `plugin` is for sharing
with a team through Claude Code.

---

## 7. Housekeeping

```bash
python3 bin/skillpp lifecycle -v     # tiers and stale references
python3 bin/skillpp reconcile        # promoted skills whose file is gone
python3 bin/skillpp check --name <n> # dependencies present? exit 2 if not
python3 bin/skillpp tier <n> cold    # move out of the loaded index
```

`reconcile` reports and never decides. A promoted skill whose file was deleted
stays out of the review queue while the file is missing; whether to propose it
again is yours.

Nothing expires on a timer. Age is the wrong signal — a procedure done four
times in June is worth more than one done once last week.

---

## 8. Turning it off

Remove the `SessionEnd` hook from `~/.claude/settings.json` (or
`.claude/settings.json`) and nothing runs at all. The store stays where it is;
promoted skills keep working, because they are ordinary Claude Code skills and
do not depend on this tool once written.

---

## 9. Facts about Claude Code this depends on

Verified here, and worth knowing before changing any of it.

- **`` !`command` `` in a `.claude/commands/` file runs *before* the model sees
  the prompt**, and its output replaces the placeholder. There is no exit-code
  branching, so "nothing to do" has to be a sentence in the output.
- **`${CLAUDE_PROJECT_DIR}` is not substituted** inside those shell
  injections — the permission checker sees a live expansion and refuses. It
  *is* available to hook commands in `settings.json`.
- **`allowed-tools` globs are literal about whitespace.**
  `Bash(python3 * skillpp *)` does not match `python3 bin/skillpp …`.
- **The transcript JSONL format is documented nowhere.** Only
  `transcript_path` is specified, which is why `transcript.py` raises rather
  than returning `[]` when a file has records but nothing recognisable.
- **A model will report a tool call it did not make.** Asked to write a file
  with `Write` unavailable, it printed "Wrote SKILL.md" and wrote nothing.
  Constrain with `--allowed-tools` rather than trusting the narration.
