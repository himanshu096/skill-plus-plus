# Removing the ledger half

**Decision, 2026-08-24: the product is the detection architecture** — the memory
store, `drain`, `/log-session`, embedding matching, `/review-candidates`. The
ledger pipeline described in README §2 and `docs/claude-code.md` is legacy.

This plan removes it. The ordering is by *blast radius*, not by size: the
installer goes first because it is the only thing that misconfigures a machine
someone else owns.

---

## What was measured

Deterministic scans (AST reachability from real entry points, disk inspection).
Numbers here are reproducible, not estimated.

| | |
| --- | --- |
| dormant subsystem | **1,441 of 6,066 lines** in `skillpp/` (23.8%) |
| commands referencing `Ledger` | **13** |
| tests touching dormant symbols | **43 of 296** (14%) |
| ledger entries on disk | **2**, both 13 Aug |
| memory-store patterns on disk | **6**, through 20 Aug |
| function-level dead code | **7 of 244 defs** — negligible |

The last row is the point: this is not scattered rot. It is one coherent half,
still tested, that stopped being written to on 13 Aug.

### Why it is dormant

`capture.py` is reachable only through `cmd_hook`, and **no skillpp hook is
installed anywhere**:

- `~/.claude/settings.json` — no `hooks` key
- `.claude/settings.json` — one hook, an inline `python3 -c` one-liner appending
  to `ended-sessions.jsonl`. Hand-written, not produced by `install.py`.

So `fold_session` is live, tested code that nothing invokes.

---

## Phase 0 — the installer is wrong, not just stale

**Done, 2026-08-24.** `HOOK_EVENTS = ("SessionEnd",)`; `cmd_hook` queues and
nothing else; queue path centralised in `skillpp/queue.py`; the hand-typed
one-liner in `.claude/settings.json` replaced with `python3 bin/skillpp hook
--event SessionEnd` (repo-relative, so no machine paths land in a tracked file).
Tests 286 → 292. Verified end to end: the command in `settings.json` banks a row
that `drain` then reads.

Kept below as the record of why.

`skillpp install --apply` wires the dead half and not the live half.
`install.py:17` points `UserPromptSubmit`/`PostToolUse`/`SessionEnd` at
`skillpp hook` → `capture.py` → ledger. It never wires the SessionEnd
queue-append that `drain` reads.

A new user following the documented install gets a filling ledger, an empty
memory store, and no queue.

**Proposed shape.** Do not delete `cmd_hook` — repurpose it. Make
`skillpp hook --event SessionEnd` perform the queue-append, and reduce
`HOOK_EVENTS` to `("SessionEnd",)`. That keeps one tested entry point, deletes
the hand-written one-liner from `.claude/settings.json`, and leaves
`install.py`'s plan/apply/backup machinery intact.

`UserPromptSubmit` and `PostToolUse` are not needed: step traces come from
`prepare.py:355 trace()` parsing the transcript, not from live capture.

| location | action |
| --- | --- |
| `skillpp/install.py:17` | `HOOK_EVENTS` → `("SessionEnd",)` |
| `skillpp/cli.py:34` `cmd_hook` | route to queue-append; drop capture dispatch |
| `.claude/settings.json` | replace one-liner with the installed hook |

**Verify:** `skillpp install` (dry run) prints exactly one hook; after `--apply`,
end a session and confirm `ended-sessions.jsonl` grows by one line.

---

## Phase 1 — doc claims that actively misinform

**Blocking nothing. Two one-line edits.**

### 1a. `docs/bakeoff.md:26` and `:233` understate our own branch

```
| Runs unattended | no — someone types `/log-session` | yes |
```

False. `drain --apply` spawns the agent by subprocess (`cli.py:576-584`) through
`config.agent_command`, whose default carries `--no-session-persistence`.

This is a row in the branch-comparison table scoring us below reality, in the
same document that carries the non-determinism caveat. Fix before anyone reads
that comparison again.

### 1b. `docs/windowing.md:18` and `:612`

Claims "nothing calls `window.py`". It has **five production callers** —
`cli.py:93,310,391,512` and `local.py:31`. The claim sits in the document's own
confidence-tiering header, so it discounts everything below it. `:612` also
contradicts `:277`, `:397`, `:415`, `:467` in the same file.

Only the `reconcile.py` half of `:18` still holds.

---

## Phase 2 — decisions that gate the deletion

Deleting `ledger.py` removes capabilities the memory store **does not have**.
These are product calls, not cleanup. Each needs an answer before Phase 3.

| # | gap | evidence | options |
| --- | --- | --- | --- |
| ~~**D1**~~ | ~~ignore / never-propose-again~~ | **Decided 2026-08-24: ported.** `reject-candidate` writes a `rejected` decision with the count at refusal. It is **never re-proposed** — sightings keep accruing as visible evidence (`turned down at 4x · done 9x since`) and `reopen-candidate` is the only way back, deliberately a person's call. An auto-return after three more sightings was built first and reverted: re-asking about something just refused is what gets the tool switched off. | done (`d87aca9`, revised) |
| ~~**D2**~~ | ~~TTL / expiry~~ | **Decided 2026-08-24: promise dropped, expiry not built.** Age is the wrong signal — a procedure done four times in June beats one done once last week — and D1 covers the real case, which is "this specific thing, not now" rather than "anything old". Six entries on disk after weeks, so there is no volume problem to solve. | done, docs only |
| ~~**D3**~~ | ~~`search`~~ | **Decided 2026-08-24: repointed.** `memory.search` scores query tokens over name (weighted double) and body. Word matching, not the embedding used for candidate matching — a person searching a remembered phrase wants that phrase, and a plausible near-miss is a worse answer than an honest empty one. | done |
| ~~**D4**~~ | ~~`dictate`~~ | **Decided 2026-08-24: kept, rebuilt.** Not ported — the ledger's version parsed prose into steps and checked completeness in code, which was right before there was a model in the loop. `dictate-skill` takes a written body and `/dictate-skill` asks what the description leaves out. Provenance reads `dictated-<date>`, and that is what lets it skip the recurrence threshold. | done |
| ~~**D5**~~ | ~~both command files~~ | **Done 2026-08-24.** `skillpp-review.md` → `/review-candidates`; `skillpp-new.md` → `/dictate-skill`, after D4 settled. They were listed as one item and were two: the second was the only interface to `dictate`, so deleting it would have decided D4 by omission. | done |

**All five are settled. Phase 3 is unblocked.**

Two of them were not the decisions the table described. D1 was mostly already
built — `load` took any action string, so a `rejected` line already dropped an
entry from the queue; what was missing was vocabulary, a command and two
display fixes. And D5 was two items wearing one row: deleting `skillpp-new.md`
would have decided D4 by omission.

**What Phase 3 must now keep**, beyond the plan's "port, do not delete" list:
`cmd_search` is repointed at the store and stays; `cmd_dictate` (ledger) is
replaced by `cmd_dictate_skill` and only the former goes. They shared a name
for one test run and the later definition silently won, so delete by line
rather than by name.

D1 was the gate, and it turned out to be mostly latent rather than missing:
`load` sets status from whatever action a decision line carries, so a
`rejected` line already dropped an entry out of the queue. What it needed was
the vocabulary, a command, and two display fixes — `render` had been grouping
everything non-candidate under "Made into skills", which would have shown a
turned-down procedure to a model as a skill that exists.

**Phase 3 consequence of D2:** `SKILLPP_TTL_DAYS` and `cmd_expire` go with
`ledger.py` as planned, and no replacement is written. `README.md` no longer
promises a TTL. `docs/claude-code.md:348` still shows `skillpp expire`; it is
left for the Phase 4 rewrite rather than patched, since that file is the
dormant path end to end.

---

## Phase 3 — the deletion

**Gated on Phase 2.** One commit, tests included.

### Delete outright

| file | lines |
| --- | --- |
| `skillpp/signals.py` | 346 |
| `skillpp/capture.py` | 344 |
| `skillpp/summary.py` | 301 |
| `skillpp/ledger.py` | 272 |
| `skillpp/normalize.py` | 121 |
| `skillpp/recurrence.py` | 57 |

Commands: `dictate` (`cli.py:795`) · `stats` (`852`) · `scaffold` (`869`) ·
`promote` (`888`) · `expire` (`995`)

Also `examples/demo.sh` — it drives `skillpp hook` into the ledger, so it demos
the dormant path.

### Port, do not delete

| command | note |
| --- | --- |
| `check` (`cli.py:1092`) | already has a ledger-free branch; delete only `--id` |
| `reconcile` (`cli.py:1032`) | `decisions.jsonl` already stores `skill_path` |
| `review` / `show` (`768`/`820`) | superseded by `/review-candidates`; confirm first |

`lifecycle.py:202` is the only live→ledger tie: one lazy `STATUS_PROMOTED`
import. `lifecycle.py` otherwise stays — `memory.py:38`, `prepare.py:48` and
`install.py:146` use it for `parse_frontmatter`.

### Dead config knobs — delete with the code

```
SKILLPP_SIMILARITY   config.py:52  0.85  -> read only at capture.py:175,287
                     live gate is hardcoded similar.py:79 THRESHOLD = 0.824
SKILLPP_TTL_DAYS     config.py:54  14    -> ledger.py:250 only
```

`SKILLPP_SIMILARITY` is the **same env var name `feat/episode-filter` uses as its
real tuning knob**. On this branch it does nothing. Anyone comparing branches by
setting it is silently fooled. Remove it or make it drive `similar.py:79`.

`SKILLPP_RECURRENCE` is **not** dead — `memory.py:350` uses it as a fallback
before `THRESHOLD`. Keep.

**Verify:** test count drops 296 → ~253; full suite green; `skillpp candidates`,
`drain` (dry run), `redraft --show` all still work.

---

## Phase 4 — rewrite the docs

**Gated on Phase 3**, so the rewrite describes what exists rather than what is
about to change.

### `docs/claude-code.md` — rewrite, do not patch

The user-facing guide is entirely the dormant path. Zero mentions of
`/log-session`, `/review-candidates`, `drain`, `record-candidate`,
`commit-session`, the memory store, or embedding matching.

- `:145-154` — a table asserting all three hooks "Confirmed working against live
  payloads". None are installed. The most misleading block in `docs/`.
- `:134` — verification snippet does `json.load(...)['hooks'].keys()`. Raises
  `KeyError`. A reader following §5 hits a traceback.
- `:80` — documents `~/.claude/skillpp/usage.json` with a purpose. **Zero
  references in the codebase.** README claims usage tracking too. Invented in
  both.
- `:337` — "at least 2 substantive steps" is `capture.py:159`, dormant. Live
  floor is 25 new messages (`config.py:17`).
- `:74-83` — the file table omits `patterns/`, `occurrences.jsonl`,
  `decisions.jsonl`, `exemplars.jsonl`, `reviews/`, `drafts/`, and the queue.

### `README.md` — rewrite §2, then audit the rest

40+ stale claims. The two that damage credibility most:

- `:389` lists semantic dedup under **Not built** — "No embeddings, because this
  runs inside a hook where a network round-trip is unacceptable" — while
  `similar.py:79` makes an embedding call the core matching decision, in `drain`,
  not a hook. The doc denies the shipped mechanism and gives a reason that no
  longer applies.
- `:404-407` "Verification status" asserts three hooks installed and firing. The
  one section a reader trusts as empirical is the most wrong.

Also: commands table omits `drain` and `record-candidate` — the product — while
listing `hook`. File tree omits all 9 live modules. Test count says 63; actual
296. `:63` promises `provisional → trusted` tiers and `:113` a sub-skill DAG;
neither has implementing code.

### Broken invocations

No `pyproject.toml`, `setup.py`, or `setup.cfg`, so bare `skillpp` is not on
PATH — only `bin/skillpp` works.

- `.claude/commands/skillpp-review.md` — 10 occurrences, and no `allowed-tools`
- `.claude/commands/skillpp-new.md:14,61,82`
- `docs/claude-code.md:223,270,322,340`

---

## Not in scope

- **`.claude/commands/` vs `commands/` duplication.** All seven pairs are
  byte-identical; `tests/test_skillpp.py:2296` enforces four. No drift, no action.
- **`docs/episode-filter.md`, `docs/benchmarks.md`** — the other branch.
- **The 61% detection variance.** Separate open thread; see *Detection is
  non-deterministic* in `HANDOVER.md`. Unaffected by this cleanup.

## Preconditions

- **13 unpushed commits** on `feat/pattern-detection`. Push or knowingly accept
  before a deletion this size.
- `tests/evals/test_locator.py` is untracked. Commit or remove it first so the
  deletion commit is clean.
