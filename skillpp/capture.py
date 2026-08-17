"""Hook handlers — the capture layer.

Wired to Claude Code hooks (README 8):

* ``UserPromptSubmit`` records stated intent, folds a span when the user
  confirms ("it works"), and starts a new span when they move on.
* ``PostToolUse`` records what actually ran.
* ``Stop`` folds a *successful* task span and keeps the session buffer —
  one session can yield several recipes.
* ``SessionEnd`` folds any leftover complete span, then deletes the buffer.
* ``SessionStart`` whispers if anything is ready for ``/skillpp-review``.

**Every handler is fail-safe.** A hook that raises could disrupt the
developer's session, so all errors are swallowed to a log file and the process
always exits 0. Capture is never worth breaking someone's work over.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .ledger import Entry, Ledger, make_id, STATUS_CANDIDATE
from .normalize import parameterize, signature
from .recurrence import find_match
from .sanitize import scrub, scrub_obj

# Tool inputs worth keeping. Anything else is recorded by name only.
_KEEP_INPUT = {
    "Bash": ("command", "description"),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "NotebookEdit": ("file_path",),
}
# Pure exploration: recorded, but never the reason a workflow is proposed.
_NOISE_TOOLS = {"Read", "Glob", "Grep", "TodoWrite", "Task", "WebFetch", "WebSearch"}

# Short user confirmations — the highest-quality "this task finished" signal.
_SUCCESS_RE = re.compile(
    r"(?i)\b("
    r"it works|that works|this works|that worked|this worked|"
    r"works now|working now|all good|"
    r"looks good|looks right|lgtm|"
    r"ship it|ship that|"
    r"perfect|nice one|"
    r"commit (?:that|it|this)|"
    r"yes[,.]? (?:that'?s|this is) (?:it|right|good|correct)"
    r")\b"
)
_TEST_RUNNER_RE = re.compile(
    r"(?i)(\bpytest\b|\bjest\b|\bvitest\b|\bmocha\b|\bphpunit\b|"
    r"\bgo\s+test\b|\bcargo\s+test\b|\bnpm\s+test\b|\byarn\s+test\b|"
    r"\bpnpm\s+test\b|\bnpm\s+run\s+test\b)")
# Gates that only pass once the work is actually done.
_GATE_RE = re.compile(
    r"(?i)(\blint\b|\beslint\b|\bruff\b|\bflake8\b|\bmypy\b|\btsc\b|"
    r"\btypecheck\b|\btype-check\b)")
# Mutations that mean *shipped*. Narrower than signals.MUTATING, which answers
# a different question ("does this reach outside the machine") and so matches
# bare tool names — `kubectl get pods` and `terraform plan` are reads, and
# reading `kubectl` alone as completion splits recipes exactly as `git status`
# did. The mutating subcommand has to be present.
_CLOSING_MUTATE_RE = re.compile(
    r"(?i)(?:^|[\s/;&|])("
    r"deploy\w*|publish|release|migrate|"
    r"terraform\s+(?:apply|destroy)|"
    r"kubectl\s+(?:apply|create|delete|rollout|scale|patch)|"
    r"helm\s+(?:install|upgrade|uninstall)|"
    r"ansible-playbook|"
    r"docker\s+push|npm\s+publish|"
    r"gh\s+(?:pr\s+create|release\s+create)"
    r")\b")
_COMMIT_RE = re.compile(r"\bgit\s+(?:commit|push)\b", re.IGNORECASE)

# Residue allowed after a confirmation before it stops being a pure
# confirmation — politeness, not a new request.
_FILLER_RE = re.compile(
    r"(?i)\b(thanks?|thank\s+you|cheers|great|cool|nice|awesome|ok|okay|"
    r"yep|yeah|yes|sure|good|now|then|please)\b|[^\w\s]")


def log_error(config: Config, message: str) -> None:
    try:
        config.root.mkdir(parents=True, exist_ok=True)
        with config.log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")
    except OSError:
        pass


def _session_file(config: Config, session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "unknown"
    return config.sessions_dir / f"{safe}.json"


def _load_session(config: Config, session_id: str) -> dict:
    path = _session_file(config, session_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "session_id": session_id,
        "started": time.time(),
        "prompts": [],
        "steps": [],
        "folded_through": 0,
        "folded_prompts": 0,
        "confirmed": False,
    }


def _save_session(config: Config, session: dict) -> None:
    path = _session_file(config, session["session_id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _failed(response) -> bool:
    """Best-effort detection of a failed tool call across payload shapes."""
    if isinstance(response, dict):
        for key in ("is_error", "isError", "error"):
            if response.get(key):
                return True
        for key in ("exit_code", "exitCode", "returncode"):
            code = response.get(key)
            if isinstance(code, int) and code != 0:
                return True
        text = " ".join(str(v) for v in response.values() if isinstance(v, str))
    else:
        text = str(response or "")
    lowered = text[:400].lower()
    return any(marker in lowered for marker in (
        "command failed", "error:", "traceback", "fatal:", "no such file",
        "permission denied", "exit code 1",
    ))


def confirmation_residue(text: str) -> str | None:
    """The non-confirmation part of a message, or None if it is not one.

    ``"looks good, thanks"`` -> ``""``  (pure confirmation)
    ``"perfect, now add a cap"`` -> ``"now add a cap"``  (carries a request)
    ``"write tests then run them"`` -> ``None``  (not a confirmation at all)
    """
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > 120:
        return None
    match = _SUCCESS_RE.search(cleaned)
    if not match:
        return None
    return (cleaned[:match.start()] + " " + cleaned[match.end():]).strip(" .,!;:?-")


def is_success_utterance(text: str) -> bool:
    """True only for a *pure* confirmation of the last task.

    Judged by what is left over rather than by length. A short trailing
    request ("perfect, now add a cap") is still a request: treating it as a
    bare confirmation folds the span and then drops the prompt, losing the
    intent for the work that follows — the one thing a raw command log can
    never recover. Politeness ("thanks") is not a request.
    """
    residue = confirmation_residue(text)
    if residue is None:
        return False
    return not _FILLER_RE.sub("", residue).strip()


def _substantive(steps: list[dict]) -> list[dict]:
    return [s for s in steps if s.get("tool") not in _NOISE_TOOLS]


def _command(step: dict) -> str:
    return str((step.get("input") or {}).get("command", ""))


def _is_closing_step(step: dict) -> bool:
    """A step that means the task finished — not one that inspects progress.

    Deliberately narrow, and deliberately not reusing the regexes in
    `signals`. Those answer other questions and are too broad for this one:
    `VERIFYING` matches `status`, `diff`, `ps`, `get`, and `MUTATING` matches
    bare `kubectl` / `terraform`. Both are how developers orient themselves
    mid-task, and reading them as completion splits one recipe into fragments
    — `edit CHANGELOG` + `git status` folding as its own "recipe" while the
    real release steps land in a second entry. Only a passing gate, a commit,
    or an actual mutation means done.
    """
    if step.get("failed"):
        return False
    if str(step.get("tool") or "") != "Bash":
        return False
    cmd = _command(step)
    return bool(
        _COMMIT_RE.search(cmd)
        or _TEST_RUNNER_RE.search(cmd)
        or _GATE_RE.search(cmd)
        or _CLOSING_MUTATE_RE.search(cmd)
    )


def looks_successful(steps: list[dict]) -> bool:
    """Whether this span looks like a finished, working task.

    Used at Stop so we do not record two stray edits as a recipe, and so a
    failed last step stays pending until the user says it works or retries.
    """
    body = _substantive(steps)
    if len(body) < 2:
        return False
    if body[-1].get("failed"):
        return False
    return any(_is_closing_step(s) for s in body)


def handle_prompt(config: Config, payload: dict) -> dict:
    """UserPromptSubmit — intent, success confirmation, or a new task."""
    session_id = str(payload.get("session_id", "unknown"))
    prompt = scrub(str(payload.get("prompt", "")).strip())
    if not prompt:
        return {"status": "empty-prompt"}
    session = _load_session(config, session_id)
    session.setdefault("cwd", payload.get("cwd", ""))
    result: dict = {"status": "recorded"}

    if is_success_utterance(prompt):
        # A pure confirmation. Nothing to record as intent — folding is the
        # whole point, and "it works" is not what the next task is about.
        session["confirmed"] = True
        result = fold_pending(config, session, force=True)
        _save_session(config, session)
        return result

    pending = _pending_steps(session)
    if looks_successful(pending):
        # They moved on after a finished task. Count it, then start a new span.
        result = fold_pending(config, session, require_success=True)

    # A message can confirm *and* ask ("perfect, now add a cap"). Fold first so
    # the cursor advances, then record only the request half against the new
    # span — the confirmation belongs to work already counted.
    residue = confirmation_residue(prompt)
    if residue is not None:
        session["confirmed"] = True
        if not result.get("id"):
            result = fold_pending(config, session, force=True)
        prompt = residue or prompt

    if len(session.setdefault("prompts", [])) < 40:
        session["prompts"].append(prompt[: config.max_field_chars])
    _save_session(config, session)
    return result


def handle_tool(config: Config, payload: dict) -> None:
    """PostToolUse — capture what ran, scrubbed before it touches disk."""
    session_id = str(payload.get("session_id", "unknown"))
    tool = str(payload.get("tool_name", "")) or "unknown"
    session = _load_session(config, session_id)
    session.setdefault("cwd", payload.get("cwd", ""))

    if len(session["steps"]) >= config.max_steps_per_session:
        return

    raw_input = payload.get("tool_input") or {}
    if not isinstance(raw_input, dict):
        raw_input = {"value": raw_input}

    # A Skill invocation is how tiering learns what is actually used (README 6).
    if tool == "Skill":
        from .lifecycle import record_use
        record_use(config, str(raw_input.get("skill", "")))
    keep = _KEEP_INPUT.get(tool)
    if keep:
        kept = {k: raw_input.get(k) for k in keep if raw_input.get(k) is not None}
    elif tool.startswith("mcp__"):
        kept = {k: v for k, v in list(raw_input.items())[:5]
                if isinstance(v, (str, int, float, bool))}
    else:
        kept = {}

    step = {
        "tool": tool,
        "input": scrub_obj(kept, config.max_field_chars),
        "failed": _failed(payload.get("tool_response")),
        "t": round(time.time(), 1),
    }
    session["steps"].append(step)
    _save_session(config, session)


def handle_stop(config: Config, payload: dict) -> dict:
    """Stop — a turn finished. Fold only if the open span looks successful.

    The buffer stays on disk so a later 'it works' or a later turn can still
    attach to a failed or incomplete span. Deleting here would shatter a
    multi-step recipe across turns.
    """
    session_id = str(payload.get("session_id", "unknown"))
    path = _session_file(config, session_id)
    if not path.exists():
        return {"status": "no-session"}
    session = _load_session(config, session_id)
    result = fold_pending(config, session, require_success=True)
    _save_session(config, session)
    return result


def handle_session_end(config: Config, payload: dict) -> dict:
    """SessionEnd — fold leftover complete work, then drop the buffer."""
    session_id = str(payload.get("session_id", "unknown"))
    path = _session_file(config, session_id)
    if not path.exists():
        return {"status": "no-session"}
    session = _load_session(config, session_id)
    ingest_transcript(session, payload.get("transcript_path"), config)
    try:
        force = bool(session.get("confirmed"))
        result = fold_pending(config, session, force=force, require_success=False)
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return result


def handle_session_start(config: Config, payload: dict | None = None) -> dict:
    """SessionStart — quiet hint if anything is waiting for review."""
    del payload
    stats = Ledger(config).stats()
    ready = int(stats.get("ready") or 0)
    if ready <= 0:
        return {"status": "quiet", "ready": 0}
    noun = "recipe" if ready == 1 else "recipes"
    message = f"Skill Plus Plus: {ready} {noun} ready — /skillpp-review"
    return {"status": "hint", "ready": ready, "message": message}


def keep_current(config: Config, session_id: str | None = None) -> dict:
    """Fold the open span now, bypassing the recurrence threshold.

    Explicit 'save this' is not noise — same rule as dictation.
    """
    if session_id:
        if not _session_file(config, session_id).exists():
            return {"status": "no-session"}
        session = _load_session(config, session_id)
    else:
        session = _latest_session(config)
        if session is None:
            return {"status": "no-session"}
    result = fold_pending(config, session, force=True, source="kept")
    _save_session(config, session)
    return result


def fold_session(config: Config, session: dict) -> dict:
    """Force-fold every step. Kept for tests and for ignore-recurrence tracking."""
    session["folded_through"] = 0
    session["folded_prompts"] = 0
    return fold_pending(config, session, force=True)


def _pending_steps(session: dict) -> list[dict]:
    start = int(session.get("folded_through") or 0)
    return list(session.get("steps") or [])[start:]


def _pending_prompts(session: dict) -> list[str]:
    start = int(session.get("folded_prompts") or 0)
    return list(session.get("prompts") or [])[start:]


def _advance_cursor(session: dict) -> None:
    session["folded_through"] = len(session.get("steps") or [])
    session["folded_prompts"] = len(session.get("prompts") or [])
    session["confirmed"] = False


def _latest_session(config: Config) -> dict | None:
    files = sorted(config.sessions_dir.glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
    return None


def fold_pending(config: Config, session: dict, *, force: bool = False,
                 require_success: bool = False, source: str = "capture") -> dict:
    """Maybe write the open span to the ledger and advance the cursor."""
    pending = _pending_steps(session)
    body = _substantive(pending)
    if len(body) < 2:
        return {"status": "too-thin", "steps": len(body)}
    if body[-1].get("failed") and not force:
        return {"status": "unsuccessful", "steps": len(body)}
    if require_success and not force and not looks_successful(pending):
        return {"status": "pending", "steps": len(body)}
    result = fold_span(config, session, body, _pending_prompts(session),
                       source=source)
    _advance_cursor(session)
    return result


def fold_span(config: Config, session: dict, steps: list[dict],
              intents: list[str], *, source: str = "capture") -> dict:
    """Turn one task span into a new or updated ledger entry."""
    cwd = session.get("cwd") or ""
    # Copy so parameterisation cannot mutate the live session buffer.
    body = json.loads(json.dumps(steps))
    for step in body:
        payload = step.get("input") or {}
        for key, value in list(payload.items()):
            if isinstance(value, str):
                payload[key] = parameterize(value, cwd)

    sig = signature(body)
    if not sig:
        return {"status": "no-signature"}

    ledger = Ledger(config)
    existing = find_match(sig, list(ledger.all()), config.similarity_threshold)
    intents = list(intents)[:5]

    deps_mcp = sorted({s["tool"] for s in body if str(s.get("tool", "")).startswith("mcp__")})
    deps_cli = sorted(_cli_dependencies(body))

    if existing:
        existing.occurrences += 1
        existing.last_seen = datetime.now(timezone.utc).replace(
            microsecond=0).isoformat()
        if cwd and cwd not in existing.projects:
            existing.projects.append(cwd)
        sid = session.get("session_id", "")
        if sid and sid not in existing.sessions:
            existing.sessions.append(sid)
        for intent in intents:
            if intent not in existing.intents:
                existing.intents.append(intent)
        del existing.intents[8:]
        if len(existing.variants) < 4:
            existing.variants.append(body)
        existing.deps_mcp = sorted(set(existing.deps_mcp) | set(deps_mcp))
        existing.deps_cli = sorted(set(existing.deps_cli) | set(deps_cli))
        if source == "kept":
            existing.source = "kept"
        ledger.save(existing)
        return {"status": "merged", "id": existing.id,
                "occurrences": existing.occurrences,
                "ready": existing.ready(config.recurrence_threshold)}

    entry = Entry(
        id=make_id(sig),
        signature=sig,
        title=_title_for(intents, body),
        status=STATUS_CANDIDATE,
        occurrences=1,
        projects=[cwd] if cwd else [],
        sessions=[session.get("session_id", "")] if session.get("session_id") else [],
        intents=intents,
        steps=body,
        variants=[body],
        deps_mcp=deps_mcp,
        deps_cli=deps_cli,
        source=source,
    )
    ledger.save(entry)
    return {"status": "created", "id": entry.id, "occurrences": 1,
            "ready": entry.ready(config.recurrence_threshold)}


def ingest_transcript(session: dict, transcript_path, config: Config) -> None:
    """Pull success phrases (and missed prompts) from a Claude Code jsonl log.

    Lexical only — this runs inside a hook. LLM judgement waits for review.
    """
    if not transcript_path:
        return
    path = Path(str(transcript_path)).expanduser()
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    existing = set(session.get("prompts") or [])
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = _user_text_from_transcript(obj)
        if not text:
            continue
        text = scrub(text)[: config.max_field_chars]
        if is_success_utterance(text):
            session["confirmed"] = True
        elif text not in existing and len(text) < 500:
            if len(session.setdefault("prompts", [])) < 40:
                session["prompts"].append(text)
                existing.add(text)


def _user_text_from_transcript(obj: dict) -> str:
    if not isinstance(obj, dict):
        return ""
    message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    role = message.get("role") if isinstance(message, dict) else None
    if obj.get("type") not in (None, "user") and role != "user":
        return ""
    if role not in (None, "user") and obj.get("type") != "user":
        return ""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        text = content.strip()
        if text.startswith("<command-") or text.startswith("<local-command"):
            return ""
        return text
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("tool_result", "tool_use"):
                return ""
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts).strip()
    return ""


# -- dictation -------------------------------------------------------------

# Split on explicit sequencing markers, plus a comma that is followed by a new
# actor or a "then" — "you search online, then give me the summary".
_STEP_SPLIT_RE = re.compile(
    r"\s*(?:->|→|=>|;|\.\s+|\bthen\b|\bafter that\b|\bnext\b|"
    r",\s*(?=(?:you|i|we|then|and then|give|send|return|check)\b))\s*",
    re.IGNORECASE)


_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_LEAD_IN_RE = re.compile(
    r"(?i)\b(return|output|reply|respond|answer|give\s+me|send|look\s+like|"
    r"format|structure|template)\b[^.]*:\s*$")
_EXAMPLE_LEAD_RE = re.compile(
    r"(?i)\b(look(?:s|ed)?\s+like|for\s+example|e\.g\.|such\s+as|"
    r"something\s+like|sample)\b")


def parse_dictation(text: str) -> list[str]:
    """Break a dictated workflow into ordered steps.

    A bulleted list is ambiguous: it can be the procedure, or it can be the
    output format introduced by "Return:". Prose alongside it decides which —
    substantial prose means the bullets are a format spec, not the steps.
    """
    text = text.strip()
    if not text:
        return []

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    bullets = [_BULLET_RE.sub("", line) for line in lines if _BULLET_RE.match(line)]
    prose = " ".join(line for line in lines if not _BULLET_RE.match(line)).strip()
    prose_body = _LEAD_IN_RE.sub("", prose).strip()

    if bullets and len(prose_body) < 40:
        return bullets  # the bullets are the procedure

    steps = [p.strip(" ,.;") for p in _STEP_SPLIT_RE.split(prose_body) if p.strip(" ,.;")]
    if bullets:
        label = "Example output" if _EXAMPLE_LEAD_RE.search(prose) else "Output format"
        steps.append(f"{label}: " + " / ".join(bullets))
    return steps or bullets


def fold_dictation(config: Config, text: str, title: str = "") -> dict:
    """Create a ledger candidate from a description the developer typed.

    Dictated candidates bypass the recurrence threshold: it exists to filter
    noise, and an explicit request is not noise.
    """
    cleaned = scrub(text.strip())
    steps = parse_dictation(cleaned)
    if not steps:
        return {"status": "empty"}

    normalized = [re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip() for s in steps]
    sig = "dictated | " + " | ".join(n for n in normalized if n)

    ledger = Ledger(config)
    existing = find_match(sig, [e for e in ledger.all() if e.source == "dictated"],
                          config.similarity_threshold)
    if existing:
        existing.occurrences += 1
        existing.last_seen = datetime.now(timezone.utc).replace(
            microsecond=0).isoformat()
        ledger.save(existing)
        return {"status": "merged", "id": existing.id, "ready": True}

    entry = Entry(
        id=make_id(sig),
        signature=sig,
        # The whole description makes a more useful title than its first step.
        title=(title or cleaned.replace("\n", " "))[:70],
        status=STATUS_CANDIDATE,
        occurrences=1,
        source="dictated",
        intents=[cleaned[: config.max_field_chars]],
        steps=[{"tool": "Stated", "input": {"text": s}} for s in steps],
    )
    ledger.save(entry)
    return {"status": "created", "id": entry.id, "ready": True,
            "steps": len(entry.steps)}


def _cli_dependencies(steps: list[dict]) -> set[str]:
    """Programs the workflow shells out to — declared deps (README 5)."""
    common = {"cd", "ls", "echo", "cat", "true", "false", "export", "source"}
    found: set[str] = set()
    for step in steps:
        if step.get("tool") != "Bash":
            continue
        command = str((step.get("input") or {}).get("command", ""))
        for chunk in command.replace("&&", ";").replace("||", ";").split(";"):
            tokens = chunk.strip().split()
            if not tokens:
                continue
            program = tokens[0]
            if "=" in program or program.startswith(("$", "(")):
                continue
            if "/" in program:
                # A path-invoked script is a file in the repo, not a PATH
                # dependency. Staleness checking covers those instead.
                continue
            if program and program not in common and program.isascii():
                found.add(program)
    return found


def _title_for(intents: list[str], steps: list[dict]) -> str:
    if intents:
        first = intents[0].strip().splitlines()[0]
        return (first[:70] + "…") if len(first) > 70 else first
    for step in steps:
        if step.get("tool") == "Bash":
            cmd = str((step.get("input") or {}).get("command", "")).strip()
            if cmd:
                return (cmd[:70] + "…") if len(cmd) > 70 else cmd
    return "captured workflow"
