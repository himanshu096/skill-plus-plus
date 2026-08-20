#!/usr/bin/env python3
"""Can an embedding model tell one procedure from another?

    python3 tests/evals/matching.py
    python3 tests/evals/matching.py --model mxbai-embed-large

Matching is the second thing detection does: given a proposal, decide whether it
is a procedure already in the store or a new one. Getting it wrong in one
direction inflates a count and silently discards a proposal; in the other it
files a duplicate the store cannot reconcile.

**This is similarity, not generation**, which is why it is worth trying locally
even though writing a body is not. A lexical `similarity()` was measured on this
exact problem earlier and returned 0.00 on every real pair — the same work gets
named and worded differently every time — so the question is whether embeddings
do what token overlap could not.

Ground truth comes from the reviews the pipeline wrote. Two bodies the frontier
judge said were the same procedure are a positive pair; two it filed separately
are a negative pair. Real sessions, real decisions, no fixtures.
"""

from __future__ import annotations

import argparse
import collections
import itertools
import json
import statistics as st
import sys
import urllib.request
from pathlib import Path

ENDPOINT = "http://127.0.0.1:11434/api/embed"
MARKERS = ("new", "another sighting of:", "merged into:")
REVIEWS = Path.home() / ".claude" / "skillpp" / "reviews"


def proposals() -> dict[str, list[tuple[str, str]]]:
    """Every proposal ever recorded, grouped by the procedure it belongs to.

    Grouped by what a match *pointed at* rather than by what the session called
    it, since the name changes between sightings and the pointer does not.
    """
    grouped: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for review in sorted(REVIEWS.glob("*.md")):
        lines = review.read_text(encoding="utf-8", errors="replace").splitlines()
        heads = [i for i, l in enumerate(lines)
                 if l.startswith("## ") and i + 1 < len(lines)
                 and any(lines[i + 1].strip().startswith(m) for m in MARKERS)]
        for n, i in enumerate(heads):
            end = heads[n + 1] if n + 1 < len(heads) else len(lines)
            after = lines[i + 1].strip()
            key = after.split(":", 1)[1].strip() if ":" in after else lines[i][3:].strip()
            body = "\n".join(lines[i + 2:end]).strip()
            if len(body) > 200:
                grouped[key].append((review.stem[:8], body))
    return dict(grouped)


def embed(text: str, model: str) -> list[float]:
    body = json.dumps({"model": model, "input": text[:8000]}).encode()
    request = urllib.request.Request(
        ENDPOINT, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)["embeddings"][0]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="nomic-embed-text")
    ap.add_argument("--show-pairs", action="store_true")
    args = ap.parse_args()

    grouped = proposals()
    if not grouped:
        print("No recorded proposals to score on.")
        return 1

    vectors = {}
    for key, items in grouped.items():
        for session, body in items:
            vectors[(key, session)] = embed(body, args.model)

    same, different = [], []
    for key, items in grouped.items():
        for (s1, _), (s2, _) in itertools.combinations(items, 2):
            same.append((cosine(vectors[(key, s1)], vectors[(key, s2)]),
                         f"{key[:30]} {s1}/{s2}"))
    keys = list(grouped)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            for (sa, _) in grouped[a]:
                for (sb, _) in grouped[b]:
                    different.append((cosine(vectors[(a, sa)], vectors[(b, sb)]),
                                      f"{a[:22]} vs {b[:22]}"))

    print(f"=== {args.model} ===\n")
    print(f"  same procedure      {len(same):>3} pairs   "
          f"min {min(s for s, _ in same):.3f}  "
          f"mean {st.mean(s for s, _ in same):.3f}  "
          f"max {max(s for s, _ in same):.3f}")
    print(f"  different procedure {len(different):>3} pairs   "
          f"min {min(s for s, _ in different):.3f}  "
          f"mean {st.mean(s for s, _ in different):.3f}  "
          f"max {max(s for s, _ in different):.3f}")

    floor = min(s for s, _ in same)
    ceiling = max(s for s, _ in different)
    print(f"\n  lowest same-pair    {floor:.3f}")
    print(f"  highest different   {ceiling:.3f}")
    if floor > ceiling:
        print(f"  SEPARABLE — any threshold in ({ceiling:.3f}, {floor:.3f}) "
              f"is right on all {len(same) + len(different)} pairs")
    else:
        overlap = [(s, w) for s, w in different if s >= floor]
        print(f"  NOT SEPARABLE — {len(overlap)} different-procedure pair(s) "
              f"score at or above the closest same-procedure pair")
        for s, w in sorted(overlap, reverse=True)[:5]:
            print(f"      {s:.3f}  {w}")

    if args.show_pairs:
        print("\n  same-procedure pairs:")
        for s, w in sorted(same, reverse=True):
            print(f"    {s:.3f}  {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
