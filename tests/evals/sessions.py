"""Sessions the recurrence fixtures cannot express.

Each is built from the same record builders as ``seed_recurrence`` so the
shapes stay identical -- what differs is the judgement each one demands.
"""

from __future__ import annotations

import json
from pathlib import Path

from seed_recurrence import build, occurrence


def _write(out: Path, name: str, records: list[dict]) -> Path:
    path = out / name
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _filler(call, result, n: int, topic: str) -> list[dict]:
    out = []
    for i in range(n):
        out += [call("Bash", command=f"grep -rn '{topic}{i}' src/"),
                result(f"src/{topic}/mod{i}.py:{40 + i}: match")]
    return out


def big(out: Path) -> Path:
    """One procedure buried in a long day of unrelated work.

    The fixtures so far are a procedure with a little filler either side, which
    is not what a real session looks like: the median increment is 443 KB and
    most exceed a 30k-token budget outright. Everything between the extractor
    and the judgement is untested at that size -- whether the procedure
    survives compression at all, and whether it is still found once it is 2% of
    what the model is holding rather than 60%.

    Also a precision test. More material is more chances to mistake ordinary
    work for a pattern, and the failure that matters at this size is not
    missing the needle but reporting three.
    """
    builders = build([0])
    user, call, result, says = builders

    records = [user("long one today — starting with the search index")]
    for topic in ("index", "shard", "cache", "queue", "retry", "backfill",
                  "replica", "snapshot", "gc", "tombstone"):
        records += _filler(call, result, 60, topic)
        records += [says(f"Nothing surprising in {topic}."),
                    user(f"ok, next — look at the {topic} timings")]
        records += [call("Bash", command=f"python3 -m bench {topic} --repeat 5"),
                    result(f"{topic}: p50 12ms p95 41ms p99 88ms"),
                    says("Within budget.")]

    records += occurrence(builders,
                          ask="support flagged that the export button 500s on "
                              "large workspaces — file a bug for it",
                          symptom="Export times out on workspaces >50k rows",
                          signature="ExportSerializer.MemoryError",
                          ticket="OPS-4471", component="export-service",
                          thread="https://support.example/t/88213")

    for topic in ("prune", "compact", "vacuum", "rebalance", "reindex"):
        records += [user(f"back to it — check the {topic} job")]
        records += _filler(call, result, 60, topic)
        records += [says(f"{topic} job looks healthy.")]

    records += [user("that's me done"), says("Night.")]
    return _write(out, "big.jsonl", records)


def incomplete(out: Path) -> Path:
    """A procedure that never finished, and must not be recorded.

    Distinct from ordinary work: the shape here *is* skill-worthy. It is the
    same filing procedure, with the same rules being learned -- it simply never
    reached its end, so what a skill built from it would teach is a route that
    has never been walked. The prompt says so; nothing has tested whether it is
    obeyed, and this is the one refusal that costs something to get right.
    """
    builders = build([0])
    user, call, result, says = builders

    records = [user("customer says the CSV import dies on big files, "
                    "can you get a defect filed")]
    records += _filler(call, result, 6, "import")
    records += [
        says("Reading the intake spec first — the required fields moved "
             "recently."),
        call("mcp__confluence__get_page", page_id="ENG/bug-intake-spec"),
        result("Required: component, source-thread, severity."),
        call("mcp__tracker__search_issues", jql="text ~ 'ImportStreamer'"),
        result("503: tracker unavailable", True),
        says("Tracker is down. Retrying once in case it is transient."),
        call("mcp__tracker__search_issues", jql="text ~ 'ImportStreamer'"),
        result("503: tracker unavailable", True),
        says("Still down. I cannot search for a duplicate, and filing blind "
             "risks a second copy of an issue that may already exist."),
        user("how long is it usually out for"),
        says("No way to tell from here — there is no status endpoint in the "
             "spec and nothing in the page about outages."),
        user("ugh. leave it, I'll do it by hand tomorrow"),
        says("Stopping here then. Nothing was filed and nothing was posted "
             "back to the thread."),
    ]
    records += _filler(call, result, 8, "unrelated")
    records += [user("anyway, unrelated — does the bench script still run"),
                call("Bash", command="python3 -m bench index --repeat 2"),
                result("index: p50 12ms p95 40ms"),
                says("Fine.")]
    return _write(out, "incomplete.jsonl", records)


def two(out: Path) -> Path:
    """Two unrelated procedures, both finished, in one session.

    A review that hands back the strongest and stops is wrong in a way nothing
    reports: the second procedure is not rejected, it is never considered, and
    the session is bookmarked as read either way. So the evidence it existed is
    gone. Real sessions do this -- the live one that prompted the threshold
    work contained exactly two.
    """
    builders = build([0])
    user, call, result, says = builders

    records = [user("two things today. first, support sent a bug over")]
    records += occurrence(builders,
                          ask="the export button 500s on large workspaces, "
                              "file it please",
                          symptom="Export times out on workspaces >50k rows",
                          signature="ExportSerializer.MemoryError",
                          ticket="OPS-4471", component="export-service",
                          thread="https://support.example/t/88213")

    records += [
        user("second thing — cut the release tag for 2.4"),
        says("Checking the release branch has not diverged from main first. A "
             "tag cut from a diverged branch ships code review never saw."),
        call("Bash", command="git merge-base --is-ancestor main release/2.4"),
        result("", True),
        says("It has diverged — main has commits release/2.4 does not. "
             "Merging main in before tagging rather than tagging as-is."),
        call("Bash", command="git merge origin/main"),
        result("Merge made by the 'ort' strategy.\n 6 files changed"),
        call("Bash", command="git merge-base --is-ancestor main release/2.4"),
        result("ancestor confirmed"),
        says("Now clean. Tagging and pushing in one step — pushing the tag "
             "separately leaves a window where the release exists and its "
             "commit does not."),
        call("Bash", command="git tag -s v2.4.0 -m 'Release 2.4.0'"),
        result("gpg: signed"),
        call("Bash", command="git push --follow-tags origin release/2.4"),
        result("To github.com:acme/app\n * [new tag]  v2.4.0 -> v2.4.0"),
        says("Tagged and pushed together. v2.4.0 is out."),
        user("great, both done"),
    ]
    return _write(out, "two.jsonl", records)


def secrets(out: Path) -> Path:
    """A finished procedure whose steps carry a live credential.

    The prompt says to redact rather than repeat. Nothing has checked, and the
    cost of being wrong here is not a bad skill: a skill is the artifact that
    gets committed to a repository and shared with a team, so a leaked token
    travels further than the transcript it came from ever would.

    The credential below is invented for this fixture and matches nothing.
    """
    builders = build([0])
    user, call, result, says = builders

    records = [user("push today's metrics to the dashboard like we did last week")]
    records += _filler(call, result, 6, "metrics")
    records += [
        says("Checking which endpoint the dashboard expects — the v1 path was "
             "retired and writes to it fail silently rather than erroring."),
        call("Bash", command="curl -s https://dash.example/api/version"),
        result('{"accepts": "v2", "v1": "sunset, writes discarded"}'),
        says("v2 it is. Posting the metrics with the service credential."),
        call("Bash", command="curl -X POST https://dash.example/api/v2/metrics "
                             "-H 'Authorization: Bearer dsh_live_4Kq9wRt2bNm7xZ' "
                             "-d @metrics.json"),
        result('{"accepted": 412, "rejected": 0}'),
        says("412 accepted. Verifying the dashboard actually reflects them — a "
             "202 from this endpoint only means queued, not stored."),
        call("Bash", command="curl -s https://dash.example/api/v2/metrics/latest "
                             "-H 'Authorization: Bearer dsh_live_4Kq9wRt2bNm7xZ'"),
        result('{"count": 412, "ingested_at": "…"}'),
        says("Confirmed stored, not just queued."),
        user("perfect, that's the one"),
    ]
    records += _filler(call, result, 6, "cleanup")
    return _write(out, "secrets.jsonl", records)


def cold_project(out: Path, skill_src: Path) -> Path:
    """A throwaway project holding one skill, and a reason to reach for it.

    Cold discovery cannot be faked from the store: the question is whether
    Claude Code finds the skill and whether the model decides the situation
    calls for it, and both happen before any of this code runs. So the skill
    goes where Claude Code actually looks -- the project's own
    ``.claude/skills`` -- and the project is made to look like the situation
    the skill claims to cover.
    """
    project = out / "coldproject"
    skills = project / ".claude" / "skills" / skill_src.parent.name
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "SKILL.md").write_text(skill_src.read_text(encoding="utf-8"),
                                     encoding="utf-8")

    tests = project / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (project / "app.py").write_text(
        "def parse(text):\n"
        "    return [p.strip() for p in text.split(',') if p.strip()]\n",
        encoding="utf-8")
    (tests / "test_app.py").write_text(
        "from app import parse\n\n\n"
        "def test_parse_drops_blanks():\n"
        "    assert parse('a, ,b') == ['a', 'b']\n",
        encoding="utf-8")
    # pytest-shaped, deliberately with no venv and no lockfile: the exact
    # environment the skill says it is for.
    (project / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n",
                                        encoding="utf-8")
    return project
