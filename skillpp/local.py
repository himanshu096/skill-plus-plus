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

ENDPOINT = "http://127.0.0.1:11434/api/generate"
PROMPTS = Path(__file__).resolve().parent / "prompts"
# Room for a model that reasons before it answers. At 128 a thinking model
# spends the whole budget and returns an empty string, which reads as a wrong
# answer rather than as a truncation.
PREDICT = 768
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


def _ask(model: str, prompt: str, *, timeout: int = 300) -> str:
    body = json.dumps({
        "model": model, "prompt": prompt, "stream": False,
        "options": {"temperature": 0, "num_predict": PREDICT},
    }).encode()
    request = urllib.request.Request(
        ENDPOINT, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LocalModelUnavailable(
            f"could not reach Ollama at {ENDPOINT}: {exc}") from exc
    if payload.get("error"):
        raise LocalModelUnavailable(str(payload["error"]))
    answer = payload.get("response", "")
    if payload.get("done_reason") == "length":
        answer += f"\n[truncated at {PREDICT} tokens before answering]"
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
