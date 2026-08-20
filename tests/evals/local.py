#!/usr/bin/env python3
"""Ask a local model the locator question, on the same fixtures and labels.

    python3 tests/evals/local.py                       # every case, one model
    python3 tests/evals/local.py --model qwen2.5:7b-ctx16k
    python3 tests/evals/local.py --case cold-bespoke --aspect tools

Free, so unlike `run.py` this can be swept without thinking about cost. It is
not free of *memory*: each model stays resident after answering, and two of
these at once took an 18 GB machine to 16.5 GB. One model per invocation, and
each is unloaded when its sweep ends.

What it cannot be casual about is faithfulness.

**The prompt is read from the command file, not written here.** `run.py` shells
out to `claude -p "/locate ..."` precisely so an eval cannot drift from the real
prompt, and a local model has no slash commands — so the prompt has to be
reconstructed. It is reconstructed *from the file*: frontmatter stripped, and
the `` !`command` `` line replaced with what that command actually prints. A
hand-copied prompt would test the copy, and the two would part company the first
time someone edited one of them.

The frontier arm is the control. A disagreement between the two on the same
fixture is the measurement; a local score on its own says nothing, because these
fixtures are small and their labels were argued over at length.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "tests" / "evals"),
                str(REPO / "tests" / "fixtures")]

import cold      # noqa: E402
import locator   # noqa: E402

OLLAMA = "http://127.0.0.1:11434/api/generate"
ALL_AT_ONCE = REPO / ".claude" / "commands" / "locate.md"
ONE_AT_A_TIME = REPO / ".claude" / "commands" / "locate-one.md"
PROMPTS = REPO / "skillpp" / "prompts"
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_BANG = re.compile(r"!`([^`]+)`")


def prompt_for(transcript: Path, aspects: tuple[str, ...] = (),
               segment: int | None = None) -> str:
    """The real command file, with its embedded command actually run.

    Mirrors what Claude Code does with `` !`…` ``: the substitution happens
    before the model sees anything, so what comes back here is the same text a
    frontier run would have been given.

    ``segment`` picks the one-at-a-time prompt, which asks about a single
    request and answers in one word. That shape exists because a 7B model given
    the all-at-once prompt returned its numbered example verbatim for three
    different fixtures — there is no numbering to copy when the question is
    about one segment.
    """
    command = ONE_AT_A_TIME if segment is not None else ALL_AT_ONCE
    text = _FRONTMATTER.sub("", command.read_text(encoding="utf-8"))
    args = [str(transcript)]
    if segment is not None:
        args += ["--only", str(segment)]
    for aspect in aspects:
        args += ["--aspect", aspect]

    def substitute(match: re.Match) -> str:
        command = match.group(1).replace("$ARGUMENTS", " ".join(args))
        done = subprocess.run(command, shell=True, cwd=REPO,
                              capture_output=True, text=True)
        return done.stdout
    return _BANG.sub(substitute, text)


def ask_claude(prompt: str, *, timeout: int = 300) -> tuple[str, float]:
    """The frontier control, on the identical prompt.

    Without it a local failure is unattributable: a wrong answer could be the
    model or could be a prompt that nobody can answer. No tools — the segment is
    already in the text.
    """
    start = time.monotonic()
    done = subprocess.run(["claude", "-p", prompt, "--no-session-persistence"],
                          cwd=REPO, capture_output=True, text=True,
                          timeout=timeout)
    return done.stdout, time.monotonic() - start


def ask(model: str, prompt: str, *, timeout: int = 300) -> tuple[str, float]:
    body = json.dumps({
        "model": model, "prompt": prompt, "stream": False,
        # Deterministic: this is a classification, and a sampled one cannot be
        # compared against a frontier run or across models.
        "options": {"temperature": 0, "num_predict": 128},
    }).encode()
    request = urllib.request.Request(
        OLLAMA, data=body, headers={"Content-Type": "application/json"})
    start = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        answer = json.load(response).get("response", "")
    return answer, time.monotonic() - start


def fixtures(out: Path) -> dict[str, tuple[Path, list[tuple[str, str]]]]:
    import their_scenarios
    built: dict[str, tuple[Path, list[tuple[str, str]]]] = {}
    for name, labels in locator.TRUTH.items():
        if name in locator.FIXTURES:
            built[name] = (locator.FIXTURES[name], labels)
        else:
            key = name.replace("their-", "")
            key = {"distinct": "distinct-tasks", "refine": "refine",
                   "retry": "retry", "explore": "explore-then-fix",
                   "mid-investigation": "mid-investigation",
                   "recurs": "recurs"}.get(key, key)
            built[name] = (their_scenarios.ALL[key](out), labels)
    for name, labels in cold.TRUTH.items():
        built[name] = (cold.ALL[name](out), labels)
    return built


_YES_NO = re.compile(r"\b(yes|no)\b", re.I)


def segment_text(transcript: Path, index: int,
                 aspects: tuple[str, ...] = ()) -> str:
    """One rendered request, without the surrounding instructions."""
    args = ["--only", str(index)]
    for aspect in aspects:
        args += ["--aspect", aspect]
    done = subprocess.run([sys.executable, "bin/skillpp", "segments",
                           str(transcript), *args],
                          cwd=REPO, capture_output=True, text=True)
    return done.stdout


def yes_no(said: str) -> bool | None:
    """Strict: a reply containing both words has not answered."""
    found = {m.group(1).lower() for m in _YES_NO.finditer(said)}
    if len(found) != 1:
        return None
    return found.pop() == "yes"


def decompose(model: str, transcript: Path, index: int,
              aspects: tuple[str, ...] = (),
              backend: str = "ollama") -> tuple[str | None, float, str]:
    """Two visible-fact questions, combined in code rather than by the model.

    `landed` is `changed && worked`. Asking both halves at once is what the
    one-call prompt does, and a 7B answered it by latching onto whichever
    corroborating signal the prompt had mentioned. One question at a time about
    a visible fact is the shape that model was measured handling well.

    The second question is skipped when nothing changed: it presupposes a
    change, and asking it anyway invites an answer about something else.
    """
    segment = segment_text(transcript, index, aspects)
    spent = 0.0
    ask_one = (lambda text: ask_claude(text)) if backend == "claude" \
        else (lambda text: ask(model, text))

    template = (PROMPTS / "changed.md").read_text(encoding="utf-8")
    reply, took = ask_one(template.replace("{SEGMENT}", segment))
    spent += took
    changed = yes_no(reply)
    if changed is None:
        return None, spent, "changed: no answer"
    if not changed:
        return "open", spent, "nothing changed"

    template = (PROMPTS / "settled.md").read_text(encoding="utf-8")
    reply, took = ask_one(template.replace("{SEGMENT}", segment))
    spent += took
    worked = yes_no(reply)
    if worked is None:
        return None, spent, "changed, worked: no answer"
    return ("landed" if worked else "open"), spent, (
        "changed and worked" if worked else "changed, did not work")


_WORD = re.compile(r"\b(landed|open)\b", re.I)


def one_word(said: str) -> str | None:
    """The verdict in a one-word reply, or None if it gave two answers.

    Strict on purpose. A reply naming both verdicts has not answered, and
    treating the first as the answer would score a hedge as a decision.
    """
    found = {m.group(1).lower() for m in _WORD.finditer(said)}
    return found.pop() if len(found) == 1 else None


def score(labels: list[tuple[str, str]], said: str) -> tuple[int, int, str]:
    """Segments right, segments asked, and what went wrong.

    An answer naming segments that do not exist scores zero regardless of which
    labelled ones it got right. Without that, a model reciting the example from
    the prompt -- three lines, whatever the session -- scores full marks on any
    single-segment fixture whose one label happens to match. That happened on
    the first local run and looked like a pass.
    """
    got = locator.parse(said)
    phantom = sorted(i for i in got if i >= len(labels))
    if phantom:
        return 0, len(labels), f"answered segments that do not exist: {phantom}"
    wrong = [f"{i}: said {got.get(i, '—')}, is {want}"
             for i, (want, _why) in enumerate(labels) if got.get(i) != want]
    return len(labels) - len(wrong), len(labels), "; ".join(wrong)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    # One model by default. Loading a second alongside the first put a 18 GB
    # machine at 16.5 GB, and a swapping model is not measuring anything.
    ap.add_argument("--model", action="append",
                    help="repeatable, but each stays resident — pass one at a "
                         "time on a machine that cannot hold both")
    ap.add_argument("--case", action="append")
    ap.add_argument("--aspect", action="append",
                    help="withhold everything else, as the cheap pass does")
    ap.add_argument("--show", action="store_true", help="print each reply")
    ap.add_argument("--decompose", action="store_true",
                    help="two visible-fact questions per request, combined in "
                         "code — the local variant")
    ap.add_argument("--per-segment", action="store_true",
                    help="one call per request via /locate-one, instead of "
                         "every verdict in one call")
    ap.add_argument("--backend", choices=("ollama", "claude"), default="ollama",
                    help="claude runs the frontier control on the identical "
                         "prompt, so a local failure is attributable")
    args = ap.parse_args()

    models = ["frontier"] if args.backend == "claude" else (
        args.model or ["qwen2.5:7b-ctx16k"])
    aspects = tuple(args.aspect or ())
    out = REPO / "tests" / "evals" / "_local"
    out.mkdir(exist_ok=True)
    built = fixtures(out)
    picked = {k: v for k, v in built.items() if not args.case or k in args.case}
    if not picked:
        print(f"no such case. have: {', '.join(built)}")
        return 2

    label = f" [{', '.join(aspects)} only]" if aspects else ""
    for model in models:
        right = total = 0
        seconds = 0.0
        print(f"\n=== {model}{label} ===")
        for name, (path, labels) in picked.items():
            try:
                if args.decompose:
                    said, took, replies = "", 0.0, []
                    for index, _ in enumerate(labels):
                        verdict, spent, why_ = decompose(
                            model, path, index, aspects, args.backend)
                        took += spent
                        replies.append(f"{index} {verdict or '(no answer)'}"
                                       f"   [{why_}]")
                        if verdict:
                            said += f"{index} {verdict}\n"
                    text = f"{len(labels)}x2 calls"
                    said_show = "\n".join(replies)
                elif args.per_segment:
                    said, took, replies = "", 0.0, []
                    for index, _ in enumerate(labels):
                        text = prompt_for(path, aspects, segment=index)
                        reply, spent = (ask_claude(text)
                                        if args.backend == "claude"
                                        else ask(model, text))
                        took += spent
                        verdict = one_word(reply)
                        replies.append(f"{index} {verdict or '(no answer)'}")
                        # Reassembled into the all-at-once shape so one scorer
                        # covers both variants.
                        said += f"{index} {verdict}\n" if verdict else ""
                    text = f"{len(labels)} calls"
                    said_show = "\n".join(replies)
                else:
                    text = prompt_for(path, aspects)
                    said, took = (ask_claude(text) if args.backend == "claude"
                                  else ask(model, text))
                    said_show = said
            except (urllib.error.URLError, TimeoutError) as exc:
                print(f"  {name:<26} backend unreachable: {exc}")
                return 1
            ok, n, why = score(labels, said)
            right += ok; total += n; seconds += took
            mark = "ok" if ok == n else f"{n - ok} wrong"
            size = text if isinstance(text, str) and text.endswith("calls") \
                else f"{len(text)//4} tok"
            print(f"  {name:<26} {ok}/{n} {mark:<10} "
                  f"{size:>10} in  {took:>5.1f}s"
                  + (f"   {why}" if why else ""))
            if args.show:
                print("".join(f"      {ln}\n"
                              for ln in said_show.strip().splitlines()))
        print(f"  total {right}/{total} segments"
              f"   {seconds:.0f}s for {len(picked)} calls")
        # Unloaded between models rather than after the last one, so a sweep
        # over two never holds both.
        if args.backend == "ollama":
            subprocess.run(["ollama", "stop", model], capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
