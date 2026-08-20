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

**What it cannot do.** It compares bodies to bodies. Raw session text scored 0.62
to 0.67 against its *own* procedure's body -- inside the different-procedure band
-- so there is no version of this that matches a session before a body has been
written for it. That is measured, not assumed.

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
