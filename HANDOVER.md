# Handover — pattern detection (`feat/pattern-detection`)

Branch: `feat/pattern-detection`, pushed, merged up to date with `origin/main`
(`f182f43`). 82 tests pass: `python3 -m unittest discover -s tests -q`.
`pytest` is not installed and there is no `pyproject.toml`; use
`uv run --with pytest pytest tests/` if you want pytest specifically.

Invoke the CLI as `python3 bin/skillpp` — bare `skillpp` is not on PATH and
will silently miss the repo's own copy.

---

## Division of labour (agreed, don't cross it)

**Their layer — categorization and lifecycle. Treat as status quo, do not
rebuild.** `ledger.py` / `cli.py` own `candidate` → `promoted` → `ignored`,
occurrence counting, `find_match`, `reconcile` for deleted skills, and
`recurrences_since_ignored`. Their `b1d788c` already implemented two things an
earlier version of our plan claimed: the promoted-skill self-heal and the
dismissed/ignore state. Their versions are better — `reconcile` reports by
default and only parks with `--apply`, on the reasoning that re-proposing a
skill the developer just deleted is the fastest way to get the tool switched
off.

**Our layer — detection.** Deciding what a *task* is, from a transcript.
Everything below.

---

## What exists and works

`/log-session` (`.claude/commands/log-session.md`) — reads a session
transcript from disk, segments it into tasks, writes one summary file to
`.claude/skillpp/memory/sessions/<YYYY-MM-DD>-<session-id>.md`. Optional
argument targets any past transcript (path or bare session id), which is also
the path any future automated pass must take.

Transcript resolution, verified live:

```bash
SLUG=$(pwd | sed 's|[/._]|-|g')
TRANSCRIPT="$HOME/.claude/projects/$SLUG/$CLAUDE_CODE_SESSION_ID.jsonl"
```

`CLAUDE_CODE_SESSION_ID` is in the environment. The slug replaces `/`, `.` and
`_` with `-` — all three; the slash-only version fails.

**Tested four times, real sessions, output in
`.claude/skillpp/memory/sessions/` (gitignored — read them, they are the
evidence):**

| Test | Size | Result |
| --- | --- | --- |
| Two distinct tasks | 115KB | Pass — separated, opening question excluded |
| Quality | 230KB | Pass — informative dead ends kept, real judgement |
| Fragmentation | 159KB | Pass — three "continue" turns became **one** task |
| Extreme | 23.8MB | **Fail** — sampled and did not say so |

The single best result: summarizing a past session, it inferred from an
*interruption* that the developer disliked an output format, and recommended
revisiting it. The developer's next prompt was exactly that. No fingerprint
could produce that.

---

## The two open problems, both measured

### 1. Extractor — this is the fix for large transcripts

Do **not** add a "disclose that you sampled" instruction. That was proposed and
correctly rejected as a workaround: it institutionalises analysing the wrong
representation.

A deterministic extractor (keep user messages and `tool_use` name+args; drop
thinking blocks, tool results, system reminders, slash-command plumbing,
image placeholders) reduces:

| Transcript | Raw | Extracted | Ratio |
| --- | --- | --- | --- |
| 236KB | 235,986 B | 2,774 B (~693 tok) | 85× |
| 7.9MB | 7,950,754 B | 70,835 B (~17.7k tok) | 112× |
| 23.8MB | 23,788,546 B | 95,425 B (~23.9k tok) | 249× |

**There is no large-session problem.** 99.6% of a transcript is noise, and the
signal is roughly constant regardless of raw size. The 23.8MB monster becomes
24k tokens.

It also fixes a second thing: across all four tests the agent improvised its
own extraction each time — three different ad-hoc `python3` parsers, one with a
false start, runs taking 1m27s–3m15s. Deterministic extraction removes both the
size ceiling and the fumbling.

Belongs in `skillpp/` as a subcommand (`skillpp extract-session`) so it is
testable Python, with `/log-session` calling it.

### 2. Cursor — no record of what has already been analysed

Two symptoms, one missing piece of state:

**Repeated `/log-session` in one session.** No cursor exists, so a second run
re-reads from the session's first prompt. Today it is merely wasteful (the
existing-file check catches it and asks before overwriting, producing a
superset rather than duplicates). It becomes a correctness bug the moment
anything counts occurrences.

**Resumed sessions.** Confirmed in real data: a conversation resumed many times
writes a *new* `session_id` per snapshot, each a growing superset of the last.
14 files were found for one conversation, with byte-identical leading message
UUIDs. Analysing each independently would inflate every count purely by how
often the terminal happened to resume.

Fix both with one mechanism: a cursor keyed by lineage (hash of the first few
message UUIDs — deterministic, immune to prompt rewording) storing the
last-analysed UUID. Analyse only the tail past it.

Known limitation to record rather than hide: slicing at a cursor can misjudge a
task that spans the boundary. Smaller cost than systematically inflating counts.

---

## Also outstanding

- **A 12-day, 4683-record "session" exists** (`ac68cada`, concept-buddy, 64 real
  prompts). Extraction makes it readable; it does not make it coherent. One
  document covering twelve days of unrelated work is not a session summary.
  Needs splitting before analysis — related to but distinct from the cursor.
- **Interface question, undecided.** Session summaries are currently standalone
  markdown that nothing reads. Do they feed the ledger as a better "this is one
  workflow" signal than fingerprints, or stay a parallel artifact proving value
  independently? This decides whether the extractor's output should be readable
  markdown or something `fold_session` can consume.
- **`.claude/settings.json`** retains one `SessionEnd` `command` hook appending
  payloads to `.claude/skillpp/memory/ended-sessions.jsonl`. It is the queue half
  of an eventual automated pass. **Unverified since it was rewritten** to resolve
  its output dir from the payload's `cwd` — exit a session and check a line
  appends.

---

## Dead ends — do not retry

- **`agent`-type hooks on `SessionEnd`.** Structurally impossible, not
  misconfigured. The emitter calls `KP({getAppState, hookInput, matchQuery,
  signal, timeoutMs})` and `KP`'s signature accepts **no `toolUseContext`**,
  while the agent branch does `if (!A) throw Error("ToolUseContext is required
  for agent hooks...")`. It fails silently because the emitter only surfaces
  failures shaped `if (!z.succeeded && z.output)`. Docs add that agent hooks are
  "experimental" and exist to "verify conditions before returning a decision" —
  a verdict-returner, while `SessionEnd` discards verdicts. `Sj6 = 1500`
  confirms the 1.5s budget (max 60s via `timeout`).
- **`command` hooks on `SessionEnd` work fine** — proven repeatedly, full payload
  including `transcript_path`.
- **Code-based segmentation and fingerprint matching** (`segment.py`,
  `recurrence.py` on the parked `segmentation-fixture` branch). Measured against
  79 real transcripts: completion markers ended only 10% of episodes,
  prompt-boundary cutting shredded continuations, and the matcher rated
  `edit:.md | bash:cd` a **178-occurrence workflow**. 151 candidates crossed the
  threshold, almost all noise. This is why detection moved to LLM reading. Their
  layer still builds on `find_match`; that tension is unresolved and worth
  raising before either side builds further.
- **`claude -p`** is not logged in on this machine, so nothing can be driven
  headlessly.

---

## Prompt-design lessons already paid for

- **"Dead ends omitted" was wrong** (fixed in `4099010`). A test-suite task's
  failures *were* the reasoning — they explained why `uv run --with pytest` was
  the answer. Distinguish noise (typos, wrong paths, re-runs) from discovery (a
  failure that ruled something out).
- **Verification is not a separate task.** Asked to "run the test suite to check
  nothing broke" after a docstring task, the agent folded it in and explained
  why. Correct — the test seed was badly designed, not the behaviour.
- **Read-only analysis is zero tasks** under the current rules, and that is
  defensible but worth revisiting: a long, valuable investigation currently logs
  as nothing.

---

## Uncommitted test artifacts (deliberately not committed)

`.claude/skills/source-api-table/`, `skillpp/normalize-api.md`,
`skillpp/recurrence-api.md` — outputs of earlier skill tests, not features.
