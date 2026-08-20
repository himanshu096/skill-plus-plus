"""Asking a local model where the work in a session landed.

The cheap half of detection. Two questions per request, each about a visible
fact, combined here rather than by the model:

    landed = something changed, and nothing says it failed

That split exists because a small model answers one narrow question well and
five at once badly -- measured in `skillpp/prompts/README.md`, along with which
models can and cannot answer in one word.

**What this does not do** is decide whether anything is worth keeping. It says
where a procedure *ended*, so the spans between those points are what an
expensive judge has to read, instead of the whole session. Nothing here reaches
a verdict about a skill.

Requires Ollama on the default port. A model that is not installed, or a daemon
that is not running, raises rather than guessing -- a silent fallback here would
report an empty session as a session with nothing in it.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .window import Segment

# Defaults; `Config` overrides both from the environment, since a developer
# may serve Ollama elsewhere or have these models under other names.
_DEFAULT_HOST = "http://127.0.0.1:11434"
ENDPOINT = _DEFAULT_HOST + "/api/generate"


def endpoint_for(config=None) -> str:
    # Written out rather than fetched with getattr: a string lookup hides the
    # dependency from a reader and from any check that greps for it, which is
    # how four config values ended up documented and unread.
    host = config.ollama_url if config is not None else _DEFAULT_HOST
    return (host or _DEFAULT_HOST).rstrip("/") + "/api/generate"
PROMPTS = Path(__file__).resolve().parent / "prompts"
# Room for a model that reasons before it answers. At 128 a thinking model
# spends the whole budget and returns an empty string, which reads as a wrong
# answer rather than as a truncation.
PREDICT = 768
# Ollama defaults num_ctx to 4096 and truncates a longer prompt *from the
# front*, which is where the instructions are. Measured on one real session, 5
# of 40 segments exceeded that once the prompt was added, so those five were
# answered from a mangled prompt with nothing reporting it. Real segments run to
# 11,940 tokens, so the context is sized per call from the prompt rather than
# fixed: allocating 16k for a 400-token question wastes memory on a machine that
# has none to spare.
CTX_FLOOR = 4_096
CTX_CEILING = 16_384
STRICT = "qwen2.5:7b"    # refuses a change nothing confirmed
LENIENT = "granite3.3:8b"  # accepts absence of failure

_YES_NO = re.compile(r"\b(yes|no)\b", re.I)


class LocalModelUnavailable(RuntimeError):
    """Ollama is not reachable, or the model is not installed."""


@dataclass
class Verdict:
    """What the cheap pass concluded about one request."""

    index: int
    landed: bool | None   # None where the models disagreed or would not answer
    why: str

    @property
    def escalates(self) -> bool:
        return self.landed is None


def _context_for(prompt: str) -> int:
    """Enough context for this prompt and its answer, rounded up in powers of two."""
    needed = len(prompt) // 4 + PREDICT
    size = CTX_FLOOR
    while size < needed and size < CTX_CEILING:
        size *= 2
    return size


def _ask(model: str, prompt: str, *, timeout: int = 300,
         endpoint: str | None = None) -> str:
    context = _context_for(prompt)
    body = json.dumps({
        "model": model, "prompt": prompt, "stream": False,
        "options": {"temperature": 0, "num_predict": PREDICT,
                    "num_ctx": context},
    }).encode()
    where = endpoint or ENDPOINT
    request = urllib.request.Request(
        where, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LocalModelUnavailable(
            f"could not reach Ollama at {where}: {exc}") from exc
    if payload.get("error"):
        raise LocalModelUnavailable(str(payload["error"]))
    answer = payload.get("response", "")
    if payload.get("done_reason") == "length":
        answer += f"\n[truncated at {PREDICT} tokens before answering]"
    # A prompt over the ceiling is still truncated, but now it says so instead
    # of being answered from whatever survived.
    if len(prompt) // 4 + PREDICT > CTX_CEILING:
        answer += (f"\n[prompt is {len(prompt)//4} tokens, over the "
                   f"{CTX_CEILING} context — it was cut]")
    return answer


def _yes_no(said: str) -> bool | None:
    hits = [m.group(1).lower() for m in _YES_NO.finditer(said)]
    if not hits:
        return None
    if len(set(hits)) == 1:
        return hits[0] == "yes"
    # A model reasoning aloud names both on its way to a conclusion; the last is
    # the conclusion. Below that length, naming both is a hedge.
    return hits[-1] == "yes" if len(said.split()) > 40 else None


def _one_model(model: str, text: str) -> tuple[bool | None, str]:
    changed = _yes_no(_ask(model, (PROMPTS / "changed.md").read_text()
                           .replace("{SEGMENT}", text)))
    if changed is None:
        return None, "no answer on whether anything changed"
    if not changed:
        return False, "nothing changed"
    worked = _yes_no(_ask(model, (PROMPTS / "settled.md").read_text()
                          .replace("{SEGMENT}", text)))
    if worked is None:
        return None, "no answer on whether it worked"
    return worked, "changed and worked" if worked else "changed, did not work"


def verdicts(segments: list[Segment], *, models: tuple[str, ...] = (STRICT,),
             render=lambda seg: seg.text) -> list[Verdict]:
    """One verdict per request, escalating where two models disagree.

    With one model there is nothing to disagree with, so nothing escalates and
    its mistakes pass through. With two, pair a strict model against a lenient
    one: they fail on different requests, so a disagreement marks the requests
    where the judgement is marginal. Two models of the same temperament agree
    confidently and wrongly instead, which is worse than either alone.
    """
    out = []
    for seg in segments:
        text = render(seg)
        answers = [_one_model(model, text) for model in models]
        landed = {a for a, _why in answers}
        if len(landed) == 1 and None not in landed:
            out.append(Verdict(seg.index, answers[0][0], answers[0][1]))
        else:
            out.append(Verdict(seg.index, None, " vs ".join(
                f"{m}: {why}" for m, (_a, why) in zip(models, answers))))
    return out


# Below this many tokens of developer requests there is nothing for a model to
# read. Measured: 3 of 12 reviewed sessions rendered to zero request tokens,
# each of them a session whose only prompts were slash commands or injected
# content. Asking a model about an empty page spends a call to be told nothing
# is there, and scores as a correct answer while proving nothing.
MIN_REQUEST_TOKENS = 20


def worth_reading(text: str) -> bool:
    """Whether a session has enough of a request in it to ask about.

    Free, and it has to run before triage rather than after: the three empty
    sessions in the labelled set were all answered correctly, which flattered
    the score by three without the model discriminating anything.
    """
    return len(text) // 4 >= MIN_REQUEST_TOKENS


def spans(verdicts: list[Verdict]) -> list[tuple[int, int]]:
    """Segment ranges a judge should read: everything up to each landing.

    A procedure ends where the work landed and begins after the previous
    landing, so the spans partition the session at those points. A request that
    escalated is kept inside the span it falls in rather than becoming a
    boundary -- an uncertain answer is a reason to show a judge more, never
    less.
    """
    out: list[tuple[int, int]] = []
    start = None
    for verdict in verdicts:
        if start is None:
            start = verdict.index
        if verdict.landed:
            out.append((start, verdict.index))
            start = None
    return out


def triage(transcript_text: str, *, model: str = STRICT,
           config=None) -> tuple[bool, str]:
    """Is this session worth a frontier call at all?

    The one local job that pays. Measured on twelve sessions the pipeline had
    already reviewed: every session containing a procedure was sent on, none was
    lost, and four of the six empty ones were skipped — which on the last drain
    would have been every call it spent.

    Errs toward sending on, and that asymmetry is the whole design. A session
    wrongly sent on costs one reading, which is what happens without this. A
    session wrongly skipped is never looked at again.

    Returns ``(worth_it, why)``. Anything unclear — no answer, model
    unreachable — is ``True``: this is a saving, and a saving that loses work is
    not one.
    """
    if not worth_reading(transcript_text):
        return False, "no developer request in it to read"
    prompt = (PROMPTS / "triage.md").read_text(encoding="utf-8").replace(
        "{SESSION}", transcript_text)
    try:
        answer = _yes_no(_ask(model, prompt, endpoint=endpoint_for(config)))
    except LocalModelUnavailable as exc:
        return True, f"local model unavailable ({exc}); sending on"
    if answer is None:
        return True, "no clear answer; sending on"
    return answer, ("looks like a procedure was carried out" if answer
                    else "looks like talk, reading, or a one-line change")


# --------------------------------------------------------------------------
# composing a body, one narrow question at a time
# --------------------------------------------------------------------------
#
# Asking a 7B to write a SKILL.md from a session in one call produced something
# worse than nothing: it transcribed the run rather than generalising it, named
# the repo and the function it happened to touch, picked a procedure the
# frontier judge had explicitly rejected as mechanical, and invented a command
# the session never ran.
#
# So the body is not written by the model. It is *assembled here* out of answers
# to questions narrow enough for a small model to get right, which is the only
# technique in this project with a track record -- the same move took the
# locator from 3 of 8 to 20 of 21.
#
#   which steps are the method?   one yes/no per step
#   what does this step look like without this run's details?   one line in, one out
#   what rule would have avoided this failure?   one sentence
#   when should this be used?   one sentence, shown only the generalised steps
#
# The last one matters: the trigger is written from the *generalised* steps
# rather than from the session, so there are no instance details in front of it
# to copy.

_STEP = re.compile(r"^\s*([$!=.])\s*(.*)$")


def _steps_and_failures(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Tool calls in order, and the ones whose result failed."""
    steps: list[str] = []
    failures: list[tuple[str, str]] = []
    last: str | None = None
    for line in text.splitlines():
        match = _STEP.match(line)
        if not match:
            continue
        marker, rest = match.groups()
        if marker == "$":
            steps.append(rest.strip())
            last = rest.strip()
        elif marker == "!" and last is not None:
            failures.append((last, rest.strip()))
    return steps, failures


def _ask_one(model: str, prompt_file: str, **slots: str) -> str:
    text = (PROMPTS / prompt_file).read_text(encoding="utf-8")
    for key, value in slots.items():
        text = text.replace("{" + key.upper() + "}", value)
    return _ask(model, text).strip()


def compose(ask: str, text: str, *, model: str = STRICT) -> str:
    """A SKILL.md body, assembled from narrow answers rather than written.

    ``ask`` is what the developer wanted; ``text`` is the rendered work. Returns
    the body, or an empty string if nothing in the work turned out to be method.
    """
    steps, failures = _steps_and_failures(text)
    if not steps:
        return ""

    method = []
    for step in steps:
        if _yes_no(_ask_one(model, "keep.md", ask=ask, step=step)):
            method.append(step)
    if not method:
        return ""

    general = []
    for step in method:
        line = _ask_one(model, "generalise.md", step=step).splitlines()[0]
        # A model that answered with prose instead of a command is dropped
        # rather than pasted: a sentence in a command block is worse than a
        # missing step.
        general.append(line.strip("` ") if len(line) < 200 else step)

    rules = []
    for step, error in failures:
        sentence = _ask_one(model, "rule.md", step=step, error=error[:300])
        first = sentence.split("\n")[0].strip()
        if first and len(first) < 300:
            rules.append(first)

    listed = "\n    ".join(general)
    trigger = _ask_one(model, "trigger.md", steps=listed).split("\n")[0].strip()

    out = [trigger if trigger.lower().startswith("use when")
           else f"Use when {trigger[0].lower() + trigger[1:]}" if trigger
           else "Use when this situation comes up again.", ""]
    out += ["## Steps", ""]
    out += [f"```\n{step}\n```" for step in general]
    if rules:
        out += ["", "## Rules learned the hard way", ""]
        out += [f"- {rule}" for rule in rules]
    return "\n".join(out) + "\n"
