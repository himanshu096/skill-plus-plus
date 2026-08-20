"""Deciding whether a proposed body is a procedure already in the store.

Local, and an embedding rather than a judgement. Measured on the proposals the
pipeline has recorded across real sessions: two bodies the frontier judge called
the same procedure score 0.873 to 0.990 in cosine, two it filed separately score
0.569 to 0.776. Separable with a gap of about a tenth, so ``THRESHOLD`` sits in
the middle of it.

**Why this is worth doing locally when writing a body is not.** Matching is
similarity; writing is generation, and a 7B was measured failing at the second.
It also removes the only judgement in this pipeline that scales with the store:
today the model is handed every recorded candidate to compare against, so the
prompt grows forever, while a dot product per candidate does not.

**What it cannot do: match a session before a body exists.** Three ways of
trying, all measured on sessions whose answer the pipeline had already recorded:

| compared | correct |
| --- | --- |
| whole session text against the stored body | 0 of 4 |
| the session's commands against the body's commands | 1 of 4 |
| the *landed segment's* commands against the body's commands | 1 of 7 |

Every score in the second and third sat between 0.48 and 0.58, inside the band
where different procedures live. Staging the comparison -- show the commands
first, escalate only on a suspected match -- needs the cheap stage to separate
something, and it separates nothing, so there is no threshold to escalate at.

The reason is visible in the inputs. A stored body prescribes about twenty
curated commands with placeholders; a session runs hundreds including every grep
and read, and narrowing to the landed segment leaves one to ten. Two command
lists for the same job -- `uv run --with pytest pytest tests/` against a body's
`pytest <path to the tests>` -- share almost nothing as text.

So matching runs after a body has been written, and the body needs a frontier
model. That is measured, not assumed.

A lexical similarity was tried on this problem first and returned 0.00 on every
real pair, because the same work is named and worded differently every time.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

ENDPOINT = "http://127.0.0.1:11434/api/embed"
MODEL = "nomic-embed-text"
# Midpoint of the measured gap: 0.776 highest different-procedure pair, 0.873
# lowest same-procedure pair.
THRESHOLD = 0.824
# Long bodies are truncated rather than refused. The distinguishing part of a
# procedure is its opening rules, and no recorded body has come close to this.
MAX_CHARS = 8000


class EmbeddingUnavailable(RuntimeError):
    """Ollama is not reachable, or the embedding model is not installed."""


@dataclass
class Match:
    name: str
    score: float

    @property
    def confident(self) -> bool:
        return self.score >= THRESHOLD


def embed(text: str, *, model: str = MODEL, timeout: int = 180) -> list[float]:
    body = json.dumps({"model": model, "input": text[:MAX_CHARS]}).encode()
    request = urllib.request.Request(
        ENDPOINT, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EmbeddingUnavailable(
            f"could not reach Ollama at {ENDPOINT}: {exc}") from exc
    if payload.get("error"):
        raise EmbeddingUnavailable(str(payload["error"]))
    vectors = payload.get("embeddings") or []
    if not vectors:
        raise EmbeddingUnavailable("no embedding came back")
    return vectors[0]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def closest(body: str, candidates: dict[str, str], *,
            model: str = MODEL) -> Match | None:
    """The stored procedure this body most resembles, or None if the store is empty.

    Returns the best match whether or not it clears the threshold, so a caller
    can log a near miss. Read ``Match.confident`` to decide, not the score.
    """
    if not candidates:
        return None
    target = embed(body, model=model)
    scored = [(cosine(target, embed(text, model=model)), name)
              for name, text in candidates.items()]
    score, name = max(scored)
    return Match(name=name, score=score)
