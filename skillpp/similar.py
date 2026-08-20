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

**Why a session cannot be matched against a body, diagnosed rather than
guessed.** Two hypotheses were tested and both were wrong: nomic's task prefixes
made no difference (0 of 4 either way), and comparing against the body's trigger
sentence alone barely moved it (1 of 4). What the numbers actually showed is that
a session sits **near-equidistant from every body** — a spread of 0.07 to 0.11
across the whole store — so nothing is being chosen between.

The signal is there; it does not survive crossing registers. Imperative prose
("run X, do not do Y because Z") and a transcript ("`$ Bash …` `= output`") land
in different regions whatever they are about. Session against session separates
all fifteen measured pairs.

So there are two comparisons here. ``closest`` matches a body against stored
bodies, with a gap of 0.097 and one threshold. ``nearest_session`` matches a
session against the sessions that produced each procedure, with a gap of 0.004
and therefore three zones: a score in the middle decides nothing and is handed to
the frontier model, which is what happened before any of this existed.

A lexical similarity was tried on this problem first and returned 0.00 on every
real pair, because the same work is named and worded differently every time.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

# Defaults; `Config` overrides both from the environment, since a developer
# may serve Ollama elsewhere or have these models under other names.
_DEFAULT_HOST = "http://127.0.0.1:11434"
ENDPOINT = _DEFAULT_HOST + "/api/embed"


def endpoint_for(config=None) -> str:
    # Written out rather than fetched with getattr: a string lookup hides the
    # dependency from a reader and from any check that greps for it, which is
    # how four config values ended up documented and unread.
    host = config.ollama_url if config is not None else _DEFAULT_HOST
    return (host or _DEFAULT_HOST).rstrip("/") + "/api/embed"
MODEL = "nomic-embed-text"
# Body against body. Midpoint of the measured gap: 0.776 highest
# different-procedure pair, 0.873 lowest same-procedure pair.
THRESHOLD = 0.824

# Session against session, which is a much narrower margin and gets three zones
# rather than one line. Measured across fifteen pairs of real sessions: two
# doing the same procedure scored 0.727 to 0.827, two doing different ones 0.638
# to 0.723. That separates every pair and leaves a gap of 0.004, which is not a
# margin anyone should round off.
#
# So a score in between decides nothing and is handed to the frontier model,
# which is what happens today anyway. Only the ends are acted on: above
# SURE_MATCH no session doing different work has ever scored, below SURE_NEW no
# session doing the same work has.
SURE_MATCH = 0.78
SURE_NEW = 0.70
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


@dataclass
class Exemplar:
    """One sighting of a procedure, as the session that did it."""

    name: str
    session: str
    vector: list[float]


@dataclass
class Nearest:
    """The closest sighting, and whether the score is decisive either way."""

    name: str
    score: float
    sessions: int  # how many exemplars that procedure has

    @property
    def matches(self) -> bool:
        return self.score >= SURE_MATCH

    @property
    def is_new(self) -> bool:
        return self.score <= SURE_NEW

    @property
    def unsure(self) -> bool:
        return not self.matches and not self.is_new


def embed(text: str, *, model: str = MODEL, timeout: int = 180,
          endpoint: str | None = None) -> list[float]:
    body = json.dumps({"model": model, "input": text[:MAX_CHARS]}).encode()
    where = endpoint or ENDPOINT
    request = urllib.request.Request(
        where, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EmbeddingUnavailable(
            f"could not reach Ollama at {where}: {exc}") from exc
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
            model: str = MODEL, config=None) -> Match | None:
    """The stored procedure this body most resembles, or None if the store is empty.

    Returns the best match whether or not it clears the threshold, so a caller
    can log a near miss. Read ``Match.confident`` to decide, not the score.
    """
    if not candidates:
        return None
    model = (config.embed_model or model) if config is not None else model
    where = endpoint_for(config)
    target = embed(body, model=model, endpoint=where)
    scored = [(cosine(target, embed(text, model=model, endpoint=where)), name)
              for name, text in candidates.items()]
    score, name = max(scored)
    return Match(name=name, score=score)


def add_exemplar(config, *, name: str, session: str, text: str,
                 model: str = MODEL) -> None:
    """Remember what a session doing this procedure looked like.

    Appended, never rewritten, like every other provenance in this store. More
    sightings is strictly better: a new session is compared against the best of
    them, so a procedure seen three times is easier to recognise than one seen
    once — which is the same reason the count exists.
    """
    chosen = (config.embed_model or model) if config is not None else model
    vector = embed(text, model=chosen, endpoint=endpoint_for(config))
    line = json.dumps({"name": name, "session": session, "vector": vector},
                      ensure_ascii=False)
    config.exemplars_file.parent.mkdir(parents=True, exist_ok=True)
    with config.exemplars_file.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def exemplars(config) -> list[Exemplar]:
    path = config.exemplars_file
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("name") and row.get("vector"):
            out.append(Exemplar(name=row["name"], session=row.get("session", ""),
                                vector=row["vector"]))
    return out


def nearest_session(config, text: str, *, model: str = MODEL) -> Nearest | None:
    """The recorded procedure whose sessions most resemble this one.

    Scored against the *best* exemplar of each procedure rather than the mean:
    the question is whether this session looks like any previous run of that
    work, and averaging a good match with an unusual one hides it.
    """
    stored = exemplars(config)
    if not stored:
        return None
    chosen = (config.embed_model or model) if config is not None else model
    target = embed(text, model=chosen, endpoint=endpoint_for(config))
    best: dict[str, float] = {}
    seen: dict[str, int] = {}
    for item in stored:
        score = cosine(target, item.vector)
        if score > best.get(item.name, -1.0):
            best[item.name] = score
        seen[item.name] = seen.get(item.name, 0) + 1
    name = max(best, key=lambda k: best[k])
    return Nearest(name=name, score=best[name], sessions=seen[name])
