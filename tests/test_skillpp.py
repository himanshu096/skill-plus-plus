"""Unit tests. Stdlib only: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillpp.capture import (
    fold_session, handle_prompt, handle_tool, handle_session_end,
    handle_stop, handle_session_start, is_success_utterance, keep_current,
    looks_successful,
)
from skillpp.config import Config
from skillpp.ledger import Entry, Ledger, make_id
from skillpp.lifecycle import check_staleness, parse_frontmatter, record_use, scan
from skillpp.normalize import normalize_command, parameterize, signature
from skillpp.recurrence import find_match, similarity
from skillpp.sanitize import scrub
from skillpp.signals import detect, effects
from skillpp.summary import check_dependencies, scaffold_skill


def bash(command: str, failed: bool = False) -> dict:
    return {"tool": "Bash", "input": {"command": command}, "failed": failed}


class TempRoot(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config = Config(self.root / "skillpp")
        self.config.ensure_dirs()

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestSanitize(unittest.TestCase):
    def test_redacts_known_token_shapes(self):
        cases = {
            "ghp_" + "a" * 36: "github-token",
            "AKIAIOSFODNN7EXAMPLE": "aws-access-key",
            "sk-ant-" + "x" * 40: "anthropic-key",
            "xoxb-123456789012-abcdefghijkl": "slack-token",
        }
        for secret, label in cases.items():
            out = scrub(f"export TOKEN={secret}")
            self.assertIn("[REDACTED", out, secret)
            self.assertNotIn(secret, out)

    def test_redacts_assignment_values_but_keeps_key(self):
        out = scrub('curl -H "api_key: hunter2supersecret"')
        self.assertIn("api_key", out)
        self.assertNotIn("hunter2supersecret", out)

    def test_redacts_connection_string_and_email(self):
        out = scrub("psql postgres://user:pw@db.example.com/app  # ping ops@corp.com")
        self.assertNotIn("db.example.com/app", out)
        self.assertNotIn("ops@corp.com", out)

    def test_preserves_ordinary_commands(self):
        cmd = "git commit -m 'fix the parser' && npm run test"
        self.assertEqual(scrub(cmd), cmd)

    def test_git_sha_is_not_treated_as_secret(self):
        sha = "a" * 40
        self.assertEqual(scrub(f"git checkout {sha}"), f"git checkout {sha}")

    def test_scrubbing_is_stable_for_signatures(self):
        a = scrub("deploy --token=" + "A1b2" * 12)
        b = scrub("deploy --token=" + "Z9y8" * 12)
        self.assertEqual(a, b, "same shape must scrub identically or dedup breaks")


class TestNormalize(unittest.TestCase):
    def test_normalize_command_keeps_subcommand(self):
        self.assertEqual(normalize_command("git commit -m 'x'"), "git commit")
        self.assertEqual(normalize_command("npm run test -- --watch"), "npm run")
        self.assertEqual(normalize_command("pytest -k auth"), "pytest")
        self.assertEqual(normalize_command("./scripts/deploy.sh prod"), "deploy.sh")
        self.assertEqual(normalize_command("FOO=1 terraform apply"), "terraform apply")

    def test_arguments_do_not_change_signature(self):
        a = signature([bash("pytest -k auth"), bash("git push")])
        b = signature([bash("pytest -k billing"), bash("git push")])
        self.assertEqual(a, b)

    def test_consecutive_duplicates_collapse(self):
        self.assertEqual(
            signature([bash("pytest"), bash("pytest"), bash("git push")]),
            signature([bash("pytest"), bash("git push")]))

    def test_parameterize_paths(self):
        out = parameterize("/proj/app/main.py and /proj/app/x", "/proj/app")
        self.assertNotIn("/proj/app", out)
        self.assertIn("${PROJECT_PATH}", out)

    def test_parameterize_ids(self):
        self.assertIn("${ID}", parameterize("aws s3 ls bucket-1234567", None))


class TestRecurrence(unittest.TestCase):
    def test_identical_signatures_match(self):
        self.assertEqual(similarity("a | b", "a | b"), 1.0)

    def test_unrelated_signatures_do_not_match(self):
        self.assertLess(similarity("bash:git commit | bash:git push",
                                   "bash:docker build | bash:kubectl apply"), 0.5)

    def test_one_extra_step_still_matches(self):
        a = "bash:npm run | bash:git add | bash:git commit | bash:git push"
        b = "bash:npm run | bash:git add | bash:git commit | bash:git push | bash:gh pr"
        self.assertGreater(similarity(a, b), 0.85)

    def test_find_match_respects_threshold(self):
        entries = [Entry(id="x", signature="bash:git commit | bash:git push")]
        self.assertIsNotNone(find_match("bash:git commit | bash:git push", entries, 0.85))
        self.assertIsNone(find_match("bash:terraform apply", entries, 0.85))


class TestLedger(TempRoot):
    def test_roundtrip_preserves_everything(self):
        entry = Entry(id="abc123", signature="bash:pytest", title="run the tests",
                      occurrences=3, steps=[bash("pytest -x")],
                      deps_cli=["pytest"], intents=["run tests before pushing"])
        ledger = Ledger(self.config)
        ledger.save(entry)
        loaded = ledger.get("abc123")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.occurrences, 3)
        self.assertEqual(loaded.steps, entry.steps)
        self.assertEqual(loaded.deps_cli, ["pytest"])

    def test_entry_file_is_human_readable(self):
        ledger = Ledger(self.config)
        ledger.save(Entry(id="abc123", signature="s", title="deploy staging",
                          steps=[bash("./deploy.sh")]))
        text = ledger.path_for("abc123").read_text()
        self.assertIn("# deploy staging", text)
        self.assertIn("`./deploy.sh`", text)

    def test_prefix_lookup(self):
        ledger = Ledger(self.config)
        ledger.save(Entry(id="deadbeef1234", signature="s"))
        self.assertIsNotNone(ledger.get("deadbe"))

    def test_expire_removes_only_stale_unapproved(self):
        ledger = Ledger(self.config)
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        ledger.save(Entry(id="old1", signature="a", last_seen=old, occurrences=1))
        ledger.save(Entry(id="ready", signature="b", last_seen=old, occurrences=5))
        ledger.save(Entry(id="kept", signature="c", last_seen=old, occurrences=1,
                          status="promoted"))
        removed = ledger.expire()
        self.assertEqual(removed, ["old1"])
        self.assertIsNotNone(ledger.get("ready"), "pending proposals survive")
        self.assertIsNotNone(ledger.get("kept"), "approved entries are never deleted")

    def test_search_finds_by_intent(self):
        ledger = Ledger(self.config)
        ledger.save(Entry(id="m1", signature="a", title="migration rollback",
                          intents=["roll back the failed migration"]))
        results = ledger.search("rollback migration")
        self.assertTrue(results)
        self.assertEqual(results[0][1].id, "m1")


class TestSignals(unittest.TestCase):
    def test_failure_then_retry_question(self):
        entry = Entry(id="x", signature="s", steps=[
            bash("terraform apply", failed=True),
            bash("terraform apply -lock=false"),
        ])
        kinds = [q.kind for q in detect(entry)]
        self.assertIn("failure_retry", kinds)

    def test_divergence_question_across_variants(self):
        entry = Entry(id="x", signature="s",
                      steps=[bash("./deploy.sh staging")],
                      variants=[[bash("./deploy.sh staging")],
                                [bash("./deploy.sh prod")]])
        kinds = [q.kind for q in detect(entry)]
        self.assertIn("divergence", kinds)

    def test_off_trace_ending_question(self):
        entry = Entry(id="x", signature="s", steps=[
            bash("npm run build"), bash("./deploy.sh prod")])
        kinds = [q.kind for q in detect(entry)]
        self.assertIn("off_trace_ending", kinds)

    def test_no_question_when_verification_present(self):
        entry = Entry(id="x", signature="s", steps=[
            bash("./deploy.sh prod"), bash("curl -f https://app/health")])
        kinds = [q.kind for q in detect(entry)]
        self.assertNotIn("off_trace_ending", kinds)

    def test_retry_suppresses_duplicate_divergence_question(self):
        """One command, one question: a retry already explains the variance."""
        entry = Entry(id="x", signature="s", steps=[
            bash("terraform apply", failed=True),
            bash("terraform apply -lock=false"),
        ], variants=[[bash("terraform apply")],
                     [bash("terraform apply -lock=false")]])
        kinds = [q.kind for q in detect(entry)]
        self.assertEqual(kinds.count("failure_retry"), 1)
        self.assertNotIn("divergence", kinds)

    def test_shell_builtins_are_not_stale_references(self):
        from skillpp.lifecycle import _referenced
        _, programs = _referenced("1. `export TOKEN=x`\n2. `terraform apply`\n")
        self.assertNotIn("export", programs)
        self.assertIn("terraform", programs)

    def test_clean_candidate_asks_nothing(self):
        entry = Entry(id="x", signature="s", steps=[
            bash("npm run lint"), bash("npm run test")])
        self.assertEqual(detect(entry), [])

    def test_effects_flag_destructive_and_writes(self):
        eff = effects([bash("rm -rf build"), bash("echo hi > out.txt"),
                       {"tool": "Write", "input": {"file_path": "src/a.py"}}])
        self.assertTrue(eff["destructive"])
        self.assertIn("out.txt", eff["writes"])
        self.assertIn("src/a.py", eff["writes"])


class TestCapture(TempRoot):
    def _session(self, commands, prompts=("do the thing",), sid="s1"):
        return {"session_id": sid, "cwd": "/proj", "prompts": list(prompts),
                "steps": [bash(c) for c in commands]}

    def test_thin_session_is_ignored(self):
        result = fold_session(self.config, self._session(["ls"]))
        self.assertEqual(result["status"], "too-thin")

    def test_recurrence_threshold_gates_readiness(self):
        cmds = ["npm run build", "./deploy.sh staging", "curl -f https://app/health"]
        first = fold_session(self.config, self._session(cmds, sid="s1"))
        self.assertEqual(first["status"], "created")
        self.assertFalse(first["ready"])

        second = fold_session(self.config, self._session(cmds, sid="s2"))
        self.assertEqual(second["status"], "merged")
        self.assertFalse(second["ready"])

        third = fold_session(self.config, self._session(cmds, sid="s3"))
        self.assertTrue(third["ready"], "3rd occurrence should surface a proposal")
        self.assertEqual(third["occurrences"], 3)
        self.assertEqual(len(list(Ledger(self.config).all())), 1, "must not duplicate")

    def test_secrets_never_reach_the_ledger(self):
        payload = {
            "session_id": "s9", "cwd": "/proj", "tool_name": "Bash",
            "tool_input": {"command": "curl -H 'Authorization: Bearer "
                                      + "A1b2C3d4" * 6 + "' https://api.example.com"},
            "tool_response": {"exit_code": 0},
        }
        handle_tool(self.config, payload)
        handle_tool(self.config, {**payload, "tool_input": {"command": "git push"}})
        handle_session_end(self.config, {"session_id": "s9"})
        text = "".join(p.read_text() for p in self.config.ledger_dir.glob("*.md"))
        self.assertNotIn("A1b2C3d4A1b2", text)
        self.assertIn("REDACTED", text)

    def test_intent_is_captured_from_prompts(self):
        handle_prompt(self.config, {"session_id": "s2", "cwd": "/p",
                                    "prompt": "roll back the bad migration"})
        for cmd in ("psql -c 'begin'", "./rollback.sh", "psql -c 'commit'"):
            handle_tool(self.config, {"session_id": "s2", "cwd": "/p",
                                      "tool_name": "Bash",
                                      "tool_input": {"command": cmd},
                                      "tool_response": {"exit_code": 0}})
        handle_session_end(self.config, {"session_id": "s2"})
        entries = list(Ledger(self.config).all())
        self.assertEqual(len(entries), 1)
        self.assertIn("roll back the bad migration", entries[0].intents)

    def test_session_buffer_is_deleted_after_fold(self):
        for cmd in ("npm ci", "npm test"):
            handle_tool(self.config, {"session_id": "s3", "tool_name": "Bash",
                                      "tool_input": {"command": cmd},
                                      "tool_response": {}})
        handle_session_end(self.config, {"session_id": "s3"})
        self.assertEqual(list(self.config.sessions_dir.glob("*.json")), [])

    def test_cli_dependencies_are_recorded(self):
        fold_session(self.config, self._session(
            ["gh pr create", "terraform apply", "cd /tmp"]))
        entry = list(Ledger(self.config).all())[0]
        self.assertIn("gh", entry.deps_cli)
        self.assertIn("terraform", entry.deps_cli)
        self.assertNotIn("cd", entry.deps_cli)

    def test_repo_scripts_are_not_path_dependencies(self):
        """./scripts/deploy.sh is a file in the repo, not a missing binary."""
        fold_session(self.config, self._session(
            ["npm run build", "./scripts/deploy.sh prod"]))
        entry = list(Ledger(self.config).all())[0]
        self.assertIn("npm", entry.deps_cli)
        self.assertNotIn("deploy.sh", entry.deps_cli)


class TestTaskSpans(TempRoot):
    """Success-gated spans: Stop keeps the buffer, 'it works' counts, keep skips 3×."""

    def _tool(self, sid, tool, **payload):
        body = {"session_id": sid, "cwd": "/proj", "tool_name": tool,
                "tool_response": {"exit_code": 0}}
        body.update(payload)
        handle_tool(self.config, body)

    def _recipe(self, sid, name="auth"):
        handle_prompt(self.config, {"session_id": sid, "cwd": "/proj",
                                    "prompt": f"write tests for {name}"})
        self._tool(sid, "Write",
                   tool_input={"file_path": f"/proj/tests/test_{name}.py"})
        self._tool(sid, "Bash", tool_input={"command": "pytest -x"})
        self._tool(sid, "mcp__cloud__get_logs",
                   tool_input={"service": "api", "since": "1h"})
        self._tool(sid, "Bash", tool_input={"command": f'git commit -m "tests {name}"'})

    def test_success_utterance_is_short_confirmation_only(self):
        self.assertTrue(is_success_utterance("it works"))
        self.assertTrue(is_success_utterance("looks good, thanks"))
        self.assertFalse(is_success_utterance("perfect, now write tests for billing"))
        self.assertFalse(is_success_utterance("write tests then run them"))

    def test_short_trailing_request_is_not_a_pure_confirmation(self):
        """A confirmation carrying a request must not swallow the request.

        Judged by leftover content, not length: "now add a cap" is 13
        characters and is still the intent for the next task.
        """
        self.assertFalse(is_success_utterance("perfect, now add a cap"))
        self.assertFalse(is_success_utterance("that works? I doubt it"))
        self.assertTrue(is_success_utterance("looks good, thanks"))

    def test_confirmation_plus_request_keeps_the_request_as_intent(self):
        from skillpp.capture import _load_session, handle_prompt, handle_stop
        p = lambda text: handle_prompt(
            self.config, {"session_id": "c1", "cwd": "/p", "prompt": text})
        t = lambda cmd: handle_tool(self.config, {
            "session_id": "c1", "cwd": "/p", "tool_name": "Bash",
            "tool_input": {"command": cmd}, "tool_response": {"exit_code": 0}})

        p("add retry logic"); t("vim client.py"); t("pytest -q")
        handle_stop(self.config, {"session_id": "c1"})
        p("perfect, now add a backoff cap")

        self.assertEqual(_load_session(self.config, "c1")["prompts"],
                         ["add retry logic", "now add a backoff cap"],
                         "the request half survives; the confirmation half does not")

    def test_orientation_commands_do_not_close_a_span(self):
        """`git status` / `git diff` are how you look around mid-task."""
        edit = {"tool": "Edit", "input": {"file_path": "a.py"}, "failed": False}
        for cmd in ("git status", "git diff", "ps aux", "kubectl get pods"):
            self.assertFalse(looks_successful([edit, bash(cmd)]),
                             f"{cmd!r} must not read as task completion")
        for cmd in ("pytest -q", "git commit -m x", "npm run lint", "./deploy.sh prod"):
            self.assertTrue(looks_successful([edit, bash(cmd)]),
                            f"{cmd!r} should close the span")

    def test_orientation_command_does_not_split_a_recipe(self):
        from skillpp.capture import handle_prompt, handle_stop
        p = lambda text: handle_prompt(
            self.config, {"session_id": "r1", "cwd": "/p", "prompt": text})
        t = lambda cmd: handle_tool(self.config, {
            "session_id": "r1", "cwd": "/p", "tool_name": "Bash",
            "tool_input": {"command": cmd}, "tool_response": {"exit_code": 0}})

        p("ship the release"); t("vim CHANGELOG.md"); t("git status")
        handle_stop(self.config, {"session_id": "r1"})
        p("now tag and push"); t("git tag v1.2.0"); t("git push --tags")
        handle_stop(self.config, {"session_id": "r1"})

        entries = list(Ledger(self.config).all())
        self.assertEqual(len(entries), 1, "one release flow, not two fragments")
        self.assertEqual(len(entries[0].steps), 4)

    def test_looks_successful_needs_a_closing_step(self):
        self.assertTrue(looks_successful([
            {"tool": "Write", "input": {"file_path": "t.py"}, "failed": False},
            bash("pytest"),
        ]))
        self.assertFalse(looks_successful([
            {"tool": "Edit", "input": {"file_path": "a.py"}, "failed": False},
            {"tool": "Edit", "input": {"file_path": "b.py"}, "failed": False},
        ]))
        self.assertFalse(looks_successful([
            bash("pytest", failed=True), bash("pytest", failed=True)]))

    def test_stop_keeps_the_buffer(self):
        self._recipe("s1")
        result = handle_stop(self.config, {"session_id": "s1"})
        self.assertEqual(result["status"], "created")
        self.assertTrue(list(self.config.sessions_dir.glob("*.json")),
                        "Stop must not delete the session buffer")

    def test_same_recipe_three_times_in_one_session_is_ready(self):
        """The test → run → MCP logs → commit loop, three turns, one chat."""
        for name in ("auth", "billing", "payments"):
            self._recipe("long", name)
            handle_stop(self.config, {"session_id": "long"})
        entries = list(Ledger(self.config).all())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].occurrences, 3)
        self.assertTrue(entries[0].ready(self.config.recurrence_threshold))

    def test_failed_stop_is_not_counted_until_it_works(self):
        handle_prompt(self.config, {"session_id": "s", "cwd": "/p",
                                    "prompt": "write tests"})
        self._tool("s", "Write", tool_input={"file_path": "/p/t.py"})
        handle_tool(self.config, {
            "session_id": "s", "cwd": "/p", "tool_name": "Bash",
            "tool_input": {"command": "pytest"},
            "tool_response": {"exit_code": 1, "is_error": True},
        })
        stopped = handle_stop(self.config, {"session_id": "s"})
        self.assertEqual(stopped["status"], "unsuccessful")
        self.assertEqual(list(Ledger(self.config).all()), [])

        self._tool("s", "Bash", tool_input={"command": "pytest"})
        confirmed = handle_prompt(self.config, {
            "session_id": "s", "cwd": "/p", "prompt": "it works"})
        self.assertEqual(confirmed["status"], "created")
        self.assertEqual(list(Ledger(self.config).all())[0].occurrences, 1)

    def test_keep_bypasses_the_threshold(self):
        self._recipe("k")
        result = keep_current(self.config, "k")
        self.assertEqual(result["status"], "created")
        self.assertTrue(result["ready"])
        entry = list(Ledger(self.config).all())[0]
        self.assertEqual(entry.source, "kept")
        self.assertTrue(entry.ready(self.config.recurrence_threshold))

    def test_session_end_reads_it_works_from_the_transcript(self):
        sid = "tr"
        self._tool(sid, "Write", tool_input={"file_path": "/proj/a.py"})
        handle_tool(self.config, {
            "session_id": sid, "cwd": "/proj", "tool_name": "Edit",
            "tool_input": {"file_path": "/proj/b.py"},
            "tool_response": {"exit_code": 0},
        })
        transcript = self.root / "session.jsonl"
        transcript.write_text(
            json.dumps({"type": "user",
                        "message": {"role": "user", "content": "it works"}}) + "\n",
            encoding="utf-8")
        result = handle_session_end(self.config, {
            "session_id": sid, "transcript_path": str(transcript)})
        self.assertEqual(result["status"], "created", result)
        self.assertEqual(list(self.config.sessions_dir.glob("*.json")), [])

    def test_session_start_stays_quiet_until_something_is_ready(self):
        self.assertEqual(handle_session_start(self.config)["status"], "quiet")
        self._recipe("a", "auth")
        keep_current(self.config, "a")
        hint = handle_session_start(self.config)
        self.assertEqual(hint["status"], "hint")
        self.assertIn("/skillpp-review", hint["message"])


class TestDictation(TempRoot):
    """The user's own example: 'I give you information, you search online
    about the facts -> give me in this format'."""

    EXAMPLE = ("I give you information, you search online about the facts "
               "-> give me in this format")

    def dictate(self, text, title=""):
        from skillpp.capture import fold_dictation
        result = fold_dictation(self.config, text, title)
        return Ledger(self.config).get(result["id"]), result

    def test_parses_into_ordered_steps(self):
        entry, _ = self.dictate(self.EXAMPLE)
        steps = [s["input"]["text"] for s in entry.steps]
        self.assertEqual(len(steps), 3)
        self.assertIn("search online", steps[1])

    def test_bypasses_recurrence_threshold(self):
        entry, _ = self.dictate(self.EXAMPLE)
        self.assertEqual(entry.source, "dictated")
        self.assertEqual(entry.occurrences, 1)
        self.assertTrue(entry.ready(self.config.recurrence_threshold),
                        "an explicit request is not noise")
        self.assertIn(entry.id, [e.id for e in Ledger(self.config).candidates(True)])

    def test_detects_format_referred_to_but_never_given(self):
        entry, _ = self.dictate(self.EXAMPLE)
        kinds = [q.kind for q in detect(entry)]
        self.assertIn("dangling_format", kinds)
        self.assertEqual(kinds[0], "dangling_format", "highest-value gap first")

    def test_detects_unconstrained_sources(self):
        entry, _ = self.dictate(self.EXAMPLE)
        self.assertIn("vague_sources", [q.kind for q in detect(entry)])

    def test_question_cap_still_applies(self):
        from skillpp.summary import questions_for
        entry, _ = self.dictate(self.EXAMPLE)
        self.assertGreater(len(detect(entry)), 3)
        self.assertEqual(len(questions_for(entry, self.config)), 3)

    def test_complete_description_asks_nothing(self):
        entry, _ = self.dictate(
            "Whenever I paste a claim, verify it against at least two primary "
            "sources. If sources conflict, say so instead of picking one. Return:\n"
            "- Claim\n- Verdict\n- Sources")
        self.assertEqual(detect(entry), [])

    def test_format_bullets_are_not_mistaken_for_steps(self):
        entry, _ = self.dictate(
            "Whenever I paste a claim, verify it against two primary sources. "
            "If it fails, say so. Return:\n- Claim\n- Verdict")
        steps = [s["input"]["text"] for s in entry.steps]
        self.assertIn("verify it against two primary sources", steps[0])
        self.assertTrue(steps[-1].startswith("Output format:"))

    def test_filled_in_example_is_not_labelled_a_format(self):
        """'it should look like: …' introduces one week's content, not a spec."""
        entry, _ = self.dictate(
            "I send a weekly update to my manager. It should look like:\n"
            "1. Researched AI tools\n2. Created documentation")
        last = entry.steps[-1]["input"]["text"]
        self.assertTrue(last.startswith("Example output:"), last)

    def test_explicit_format_keeps_the_format_label(self):
        entry, _ = self.dictate(
            "Whenever I paste a claim, verify it against two primary sources. "
            "If it fails say so. Return in this format:\n- Claim\n- Verdict")
        last = entry.steps[-1]["input"]["text"]
        self.assertTrue(last.startswith("Output format:"), last)

    def test_bullets_alone_are_the_procedure(self):
        entry, _ = self.dictate("1. run the linter\n2. fix what it finds\n3. open a PR")
        steps = [s["input"]["text"] for s in entry.steps]
        self.assertEqual(steps, ["run the linter", "fix what it finds", "open a PR"])

    def test_repeat_dictation_merges(self):
        first, _ = self.dictate(self.EXAMPLE)
        _, result = self.dictate(self.EXAMPLE)
        self.assertEqual(result["status"], "merged")
        self.assertEqual(len(list(Ledger(self.config).all())), 1)

    def test_secrets_in_dictation_are_scrubbed(self):
        entry, _ = self.dictate("call the API with token ghp_" + "b" * 36 + " then log it")
        self.assertNotIn("ghp_bbbb", json.dumps(entry.steps))

    def test_scaffold_works_for_dictated_entries(self):
        entry, _ = self.dictate(self.EXAMPLE)
        text = scaffold_skill(entry, "fact-check", "Verify claims against sources.",
                              answers={"when_to_use": "When the user pastes a claim."})
        fm = parse_frontmatter(text)
        self.assertEqual(fm["name"], "fact-check")
        self.assertIn("search online", text)
        self.assertIn("## Known gaps", text)
        self.assertIn("# Fact Check", text, "raw dictation is a poor heading")

    def test_when_to_use_reaches_frontmatter(self):
        """Claude Code's discovery step reads only name+description from
        frontmatter before the body ever loads. A trigger condition confirmed
        only to ## When to use in the body cannot influence that decision."""
        entry, _ = self.dictate(self.EXAMPLE)
        text = scaffold_skill(entry, "fact-check", "Verify claims.",
                              answers={"when_to_use": "When the user pastes a claim."})
        fm = parse_frontmatter(text)
        self.assertEqual(fm["when_to_use"], "When the user pastes a claim.")
        self.assertIn("## When to use", text, "the body copy stays too")

    def test_trigger_alias_reaches_frontmatter_and_body(self):
        """Gap-closing honours the aliases, so consumption must too — otherwise
        `trigger` marks the question answered while the content lands nowhere."""
        entry, _ = self.dictate(self.EXAMPLE)
        for key in ("when_to_use", "trigger"):
            text = scaffold_skill(entry, "fact-check", "Verify claims.",
                                  answers={key: "When a claim is pasted."})
            fm = parse_frontmatter(text)
            self.assertEqual(fm.get("when_to_use"), "When a claim is pasted.",
                             f"answered under {key!r}")
            self.assertNotIn("TODO", text, f"body placeholder left under {key!r}")
            self.assertNotIn("## Judgement", text, "trigger must not be a third copy")

    def test_missing_when_to_use_is_not_placeholder_in_frontmatter(self):
        """The unresolved-trigger placeholder belongs in the body/## Known
        gaps, never presented in frontmatter as if it were a real answer."""
        entry, _ = self.dictate(self.EXAMPLE)
        text = scaffold_skill(entry, "fact-check", "Verify claims.")
        fm = parse_frontmatter(text)
        self.assertNotIn("when_to_use", fm)

    def test_answering_a_question_closes_its_gap(self):
        entry, _ = self.dictate(self.EXAMPLE)
        text = scaffold_skill(entry, "fact-check", "Verify claims.", answers={
            "when_to_use": "When the user pastes a claim.",
            "output_format": "Claim / Verdict / Sources",
            "sources": "two independent primary sources",
        })
        gaps = text.split("## Known gaps")[-1] if "## Known gaps" in text else ""
        self.assertNotIn("refer to a format", gaps, "answered gap must not reappear")
        self.assertNotIn("When should this fire", gaps)
        self.assertNotIn("good enough source", gaps)
        self.assertIn("nothing to report", gaps, "unanswered gap stays open")

    def test_answering_everything_removes_the_gaps_section(self):
        entry, _ = self.dictate(self.EXAMPLE)
        text = scaffold_skill(entry, "fact-check", "Verify claims.", answers={
            "when_to_use": "When a claim is pasted.",
            "output_format": "table",
            "sources": "two primary",
            "failure": "say what could not be verified",
        })
        self.assertNotIn("## Known gaps", text)


class TestScaffold(unittest.TestCase):
    def test_scaffold_has_valid_frontmatter_and_deps(self):
        entry = Entry(id="abc", signature="s", title="deploy to staging",
                      occurrences=3, steps=[bash("./deploy.sh staging")],
                      deps_cli=["gh"], deps_mcp=["mcp__github__create_pr"])
        text = scaffold_skill(entry, "deploy-staging", "Deploy to staging.")
        fm = parse_frontmatter(text)
        self.assertEqual(fm["name"], "deploy-staging")
        self.assertEqual(fm["metadata"]["requires_cli"], ["gh"])
        self.assertEqual(fm["metadata"]["tier"], "provisional")
        self.assertIn("provenance", fm["metadata"])

    def test_unanswered_questions_become_known_gaps(self):
        entry = Entry(id="abc", signature="s", title="deploy",
                      steps=[bash("npm run build"), bash("./deploy.sh prod")])
        text = scaffold_skill(entry, "deploy", "Deploy.")
        self.assertIn("## Known gaps", text)

    def test_destructive_steps_are_called_out(self):
        entry = Entry(id="abc", signature="s", title="reset",
                      steps=[bash("rm -rf dist"), bash("npm run build")])
        self.assertIn("## Destructive operations", scaffold_skill(entry, "reset"))


class TestDependencyCheck(unittest.TestCase):
    def test_missing_cli_is_reported(self):
        result = check_dependencies(["definitely-not-a-real-binary-xyz"], [])
        self.assertFalse(result["ok"])
        self.assertEqual(result["missing_cli"], ["definitely-not-a-real-binary-xyz"])

    def test_present_cli_passes(self):
        self.assertTrue(check_dependencies(["python3"], [])["ok"])

    def test_missing_mcp_server_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = check_dependencies([], ["mcp__github__create_pr"], Path(tmp))
        self.assertFalse(result["ok"])
        self.assertIn("mcp__github__create_pr", result["missing_mcp"])

    def test_configured_mcp_server_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".mcp.json").write_text(
                json.dumps({"mcpServers": {"github": {"command": "x"}}}))
            result = check_dependencies([], ["mcp__github__create_pr"], Path(tmp))
        self.assertTrue(result["ok"])


class TestLifecycle(TempRoot):
    def _write_skill(self, directory: Path, name: str, body: str = "") -> Path:
        skill_dir = directory / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = skill_dir / "SKILL.md"
        path.write_text(
            f"---\nname: {name}\ndescription: \"test\"\nmetadata:\n"
            f"  requires_cli: [\"git\"]\n  tier: \"provisional\"\n---\n\n{body}",
            encoding="utf-8")
        return path

    def test_scan_reports_tiers_and_usage(self):
        hot = self.root / "skills"
        self._write_skill(hot, "alpha")
        self._write_skill(self.config.cold_dir, "beta")
        record_use(self.config, "alpha")
        record_use(self.config, "alpha")
        skills = {s.name: s for s in scan(hot, self.config, self.root)}
        self.assertEqual(skills["alpha"].tier, "hot")
        self.assertEqual(skills["alpha"].uses, 2)
        self.assertEqual(skills["beta"].tier, "cold")
        self.assertEqual(skills["beta"].uses, 0)

    def test_demotion_moves_files_and_never_deletes(self):
        hot = self.root / "skills"
        self._write_skill(hot, "alpha")
        skill = [s for s in scan(hot, self.config, self.root) if s.name == "alpha"][0]
        from skillpp.lifecycle import move_tier
        dest = move_tier(skill, "cold", hot, self.config)
        self.assertTrue((dest / "SKILL.md").exists())
        self.assertFalse((hot / "alpha").exists())
        self.assertEqual(
            [s.tier for s in scan(hot, self.config, self.root) if s.name == "alpha"],
            ["cold"])

    def test_staleness_detects_missing_reference(self):
        hot = self.root / "skills"
        path = self._write_skill(hot, "gamma", "Run `./scripts/gone.sh` to deploy.\n")
        self.assertTrue(any("gone.sh" in ref for ref in check_staleness(path, self.root)))

    def test_staleness_ignores_existing_reference(self):
        hot = self.root / "skills"
        (self.root / "scripts").mkdir(parents=True, exist_ok=True)
        (self.root / "scripts" / "here.sh").write_text("#!/bin/sh\n")
        path = self._write_skill(hot, "delta", "Run `./scripts/here.sh` to deploy.\n")
        self.assertEqual(
            [r for r in check_staleness(path, self.root) if "here.sh" in r], [])


class TestInstall(unittest.TestCase):
    def test_hook_command_does_not_redirect_the_ledger_into_the_repo(self):
        from skillpp.install import hook_command
        cmd = hook_command(python="/usr/bin/python3", package_root=Path("/repo"))
        self.assertIn('PYTHONPATH="/repo"', cmd)
        self.assertNotIn("--root", cmd)

    def test_plan_preserves_existing_hooks(self):
        from skillpp.install import plan_settings
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "model": "opus",
                "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "existing.sh"}]}]}}))
            merged, changes = plan_settings(path)
            on_disk = json.loads(path.read_text())
        self.assertEqual(merged["model"], "opus", "unrelated settings survive")
        self.assertIn("existing.sh", json.dumps(merged["hooks"]["PostToolUse"]))
        self.assertEqual(len(merged["hooks"]["PostToolUse"]), 2)
        self.assertEqual(len(on_disk["hooks"]["PostToolUse"]), 1, "planning must not write")

    def test_bundle_matches_the_plugin_layout_desktop_uses(self):
        from skillpp.install import build_plugin_bundle
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "skills" / "alpha"
            src.mkdir(parents=True)
            (src / "SKILL.md").write_text("---\nname: alpha\n---\nbody\n")
            out = Path(tmp) / "bundle"
            result = build_plugin_bundle([src / "SKILL.md"], out, "my-skills",
                                         "test bundle")
            manifest = json.loads((out / ".claude-plugin" / "plugin.json").read_text())
            self.assertEqual(manifest["name"], "my-skills")
            self.assertEqual(manifest["version"], "1.0.0")
            self.assertTrue((out / "skills" / "alpha" / "SKILL.md").exists())
            self.assertEqual(result["skills"], ["alpha"])

    def _skill(self, tmp, name, description):
        d = Path(tmp) / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f'---\nname: {name}\ndescription: "{description}"\n---\nbody\n')
        return d / "SKILL.md"

    def test_upload_zip_has_the_skill_folder_as_root(self):
        import zipfile
        from skillpp.install import build_upload_bundle
        with tempfile.TemporaryDirectory() as tmp:
            md = self._skill(tmp, "alpha", "does a thing")
            archive = build_upload_bundle(md, Path(tmp) / "out")
            names = zipfile.ZipFile(archive).namelist()
        self.assertIn("alpha/SKILL.md", names)
        self.assertNotIn("SKILL.md", names, "files must not sit at archive root")

    def test_upload_validation_catches_overlong_description(self):
        from skillpp.install import validate_for_upload, UPLOAD_DESCRIPTION_MAX
        with tempfile.TemporaryDirectory() as tmp:
            md = self._skill(tmp, "alpha", "x" * (UPLOAD_DESCRIPTION_MAX + 20))
            problems = validate_for_upload(md)
        self.assertTrue(problems)
        self.assertIn("trim by 20", problems[0])

    def test_upload_validation_passes_a_good_skill(self):
        from skillpp.install import validate_for_upload
        with tempfile.TemporaryDirectory() as tmp:
            md = self._skill(tmp, "alpha", "Use when the user asks for a thing.")
            self.assertEqual(validate_for_upload(md), [])

    def test_plan_is_idempotent(self):
        from skillpp.install import plan_settings
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            merged, _ = plan_settings(path)
            path.write_text(json.dumps(merged))
            _, changes = plan_settings(path)
        self.assertTrue(all("no change" in c for c in changes), changes)

    def test_stop_and_session_start_are_wired(self):
        from skillpp.install import HOOK_EVENTS, plan_settings
        self.assertIn("Stop", HOOK_EVENTS)
        self.assertIn("SessionStart", HOOK_EVENTS)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            merged, _ = plan_settings(path)
        self.assertIn("Stop", merged["hooks"])
        self.assertIn("SessionStart", merged["hooks"])


class TestReconcile(TempRoot):
    """Deleting a promoted skill must not silently retire the workflow."""

    def _promoted(self, path, title="deploy the api"):
        """A workflow that recurred to threshold, was promoted, then deleted.

        Occurrences are preserved across the reopen — the workflow already
        proved itself, so it should not have to re-accumulate from zero.
        """
        from skillpp.capture import fold_session
        cmds = ["npm run build", "./deploy.sh prod", "curl -f https://app/health"]
        for n in range(3):
            fold_session(self.config, {"session_id": f"s{n}", "cwd": "/proj",
                                       "prompts": [title],
                                       "steps": [bash(c) for c in cmds]})
        ledger = Ledger(self.config)
        entry = list(ledger.all())[0]
        entry.status = "promoted"
        entry.skill_path = str(path)
        entry.promoted_at = "2026-08-12T10:00:00+00:00"
        ledger.save(entry)
        return ledger, entry

    def test_reconcile_reports_but_never_changes_status(self):
        """Deleting a skill is usually deliberate — do not second-guess it."""
        from skillpp.lifecycle import reconcile
        ledger, entry = self._promoted(self.root / "skills" / "gone" / "SKILL.md")

        result = reconcile(ledger, self.root / "skills", self.config)
        self.assertEqual([e.id for e in result["missing"]], [entry.id])
        self.assertEqual(ledger.get(entry.id).status, "promoted",
                         "reconcile must not mutate the ledger")

    def test_present_skill_is_left_alone(self):
        from skillpp.lifecycle import reconcile
        d = self.root / "skills" / "deploy-api"
        d.mkdir(parents=True)
        md = d / "SKILL.md"
        md.write_text('---\nname: deploy-api\ndescription: "x"\n---\nbody\n')
        ledger, entry = self._promoted(md)

        result = reconcile(ledger, self.root / "skills", self.config)
        self.assertEqual([e.id for e in result["ok"]], [entry.id])
        self.assertEqual(result["missing"], [])

    def test_reopen_puts_it_back_in_the_queue(self):
        from skillpp.cli import main
        ledger, entry = self._promoted(self.root / "skills" / "gone" / "SKILL.md")
        self.assertEqual(ledger.candidates(True), [], "promoted, so not proposed")

        self.assertEqual(main(["--root", str(self.config.root), "reopen", entry.id]), 0)
        back = ledger.get(entry.id)
        self.assertEqual(back.status, "candidate")
        self.assertEqual(back.skill_path, "", "stale path cleared")
        self.assertIn(entry.id, [c.id for c in ledger.candidates(True)],
                      "occurrences are preserved, so it surfaces at once")

    def test_ignore_then_reopen_round_trip(self):
        """Suppression lives at the surfacing layer, not the matching layer."""
        from skillpp.cli import main
        from skillpp.recurrence import find_match
        ledger, entry = self._promoted(self.root / "skills" / "gone" / "SKILL.md")

        main(["--root", str(self.config.root), "ignore", entry.id])
        ignored = ledger.get(entry.id)
        self.assertEqual(ignored.status, "ignored")
        self.assertNotIn(entry.id, [c.id for c in ledger.candidates(True)],
                         "ignored workflows are never proposed")
        self.assertIsNotNone(
            find_match(ignored.signature, list(ledger.all()), 0.85),
            "but they must still match, or a recurrence would overwrite them")

        main(["--root", str(self.config.root), "reopen", entry.id])
        self.assertEqual(ledger.get(entry.id).status, "candidate")
        self.assertIn(entry.id, [c.id for c in ledger.candidates(True)],
                      "reopening puts it back in the queue")

    def test_ignoring_survives_the_workflow_recurring(self):
        """Ids derive from the signature, so a skipped match would overwrite
        the ignored entry with a fresh candidate — silently undoing the user."""
        from skillpp.capture import fold_session
        from skillpp.cli import main
        ledger, entry = self._promoted(self.root / "skills" / "gone" / "SKILL.md")
        main(["--root", str(self.config.root), "ignore", entry.id])

        cmds = ["npm run build", "./deploy.sh prod", "curl -f https://app/health"]
        fold_session(self.config, {"session_id": "later", "cwd": "/proj",
                                   "prompts": ["deploy the api"],
                                   "steps": [bash(c) for c in cmds]})

        after = ledger.get(entry.id)
        self.assertEqual(after.status, "ignored", "the ignore must hold")
        self.assertEqual(after.occurrences, 4, "but the recurrence still counts")
        self.assertEqual(len(list(ledger.all())), 1, "no duplicate entry")
        self.assertNotIn(entry.id, [c.id for c in ledger.candidates(True)])

    def test_recurring_while_ignored_is_flagged_after_threshold(self):
        from skillpp.capture import fold_session
        from skillpp.cli import main
        ledger, entry = self._promoted(self.root / "skills" / "gone" / "SKILL.md")
        main(["--root", str(self.config.root), "ignore", entry.id])
        cmds = ["npm run build", "./deploy.sh prod", "curl -f https://app/health"]

        for n in range(2):
            fold_session(self.config, {"session_id": f"x{n}", "cwd": "/proj",
                                       "prompts": ["deploy"], "steps": [bash(c) for c in cmds]})
        mid = ledger.get(entry.id)
        self.assertEqual(mid.recurrences_since_ignored, 2)
        self.assertFalse(mid.ignore_looks_wrong(self.config.recurrence_threshold))

        fold_session(self.config, {"session_id": "x2", "cwd": "/proj",
                                   "prompts": ["deploy"], "steps": [bash(c) for c in cmds]})
        flagged = ledger.get(entry.id)
        self.assertEqual(flagged.recurrences_since_ignored, 3)
        self.assertTrue(flagged.ignore_looks_wrong(self.config.recurrence_threshold),
                        "3 recurrences since ignoring is evidence the ignore was wrong")
        self.assertEqual(flagged.status, "ignored", "flagged, but still not proposed")

    def test_deleted_skill_parks_in_the_ignore_list(self):
        from skillpp.cli import main
        ledger, entry = self._promoted(self.root / "skills" / "gone" / "SKILL.md")
        main(["--root", str(self.config.root), "reconcile",
              "--skills-dir", str(self.root / "skills"), "--apply"])
        parked = ledger.get(entry.id)
        self.assertEqual(parked.status, "ignored")
        self.assertEqual(parked.skill_path, "")
        self.assertEqual(parked.ignored_at_occurrences, parked.occurrences)
        self.assertIn("Skill deleted", parked.notes)

    def test_legacy_ignored_entry_reports_no_false_recurrences(self):
        """Entries ignored before tracking existed must not report a number."""
        ledger, entry = self._promoted(self.root / "skills" / "gone" / "SKILL.md")
        entry.status = "dismissed"          # pre-rename, no ignored_at_occurrences
        ledger.save(entry)
        stale = ledger.get(entry.id)
        self.assertEqual(stale.recurrences_since_ignored, 0)
        self.assertFalse(stale.ignore_looks_wrong(3))

    def test_ignored_listing_is_visible(self):
        from skillpp.cli import main
        ledger, entry = self._promoted(self.root / "skills" / "gone" / "SKILL.md")
        main(["--root", str(self.config.root), "ignore", entry.id, "--note", "not worth it"])
        listed = [e for e in ledger.all() if e.status == "ignored"]
        self.assertEqual([e.id for e in listed], [entry.id])
        self.assertEqual(listed[0].notes, "not worth it")

    def test_orphaned_skill_is_reported_not_touched(self):
        from skillpp.lifecycle import reconcile
        d = self.root / "skills" / "stray"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            '---\nname: stray\ndescription: "x"\nmetadata:\n'
            '  provenance: "ledger:doesnotexist"\n---\nbody\n')
        result = reconcile(Ledger(self.config), self.root / "skills", self.config)
        self.assertEqual([s.name for s in result["orphaned"]], ["stray"])
        self.assertTrue((d / "SKILL.md").exists(), "never deletes a skill file")


class TestSpanBudget(TempRoot):
    """A span that never closes must not grow into an afternoon."""

    def _prompt(self, text, sid="b1"):
        from skillpp.capture import handle_prompt
        return handle_prompt(self.config, {"session_id": sid, "cwd": "/p",
                                           "prompt": text})

    def _run(self, cmd, sid="b1", failed=False):
        handle_tool(self.config, {
            "session_id": sid, "cwd": "/p", "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "tool_response": {"exit_code": 1 if failed else 0}})

    def test_unclosed_span_is_abandoned_at_the_prompt_budget(self):
        from skillpp.capture import _load_session
        statuses = []
        for n in range(self.config.max_span_prompts + 1):
            statuses.append(self._prompt(f"question {n}")["status"])
            self._run(f"grep -r thing{n} .")   # never a closing step
            self._run(f"cat file{n}.py")

        self.assertIn("abandoned", statuses,
                      f"budget of {self.config.max_span_prompts} prompts never fired: {statuses}")
        self.assertEqual(list(Ledger(self.config).all()), [],
                         "exploration is discarded, not banked as a recipe")
        session = _load_session(self.config, "b1")
        self.assertLess(session["span_prompts"], self.config.max_span_prompts,
                        "budget resets after abandoning")

    def test_unclosed_span_is_abandoned_at_the_step_budget(self):
        self._prompt("dig into this")
        for n in range(self.config.max_span_steps + 1):
            self._run(f"grep -r pattern{n} .")
        result = self._prompt("next question")
        self.assertEqual(result["status"], "abandoned")
        self.assertEqual(list(Ledger(self.config).all()), [])

    def test_a_span_that_closes_in_time_still_folds(self):
        self._prompt("add retry logic")
        self._run("vim client.py")
        self._run("pytest -q")
        result = self._prompt("now something else")
        self.assertEqual(result["status"], "created")
        entry = list(Ledger(self.config).all())[0]
        self.assertEqual(len(entry.steps), 2, "the recipe, nothing more")

    def test_budget_does_not_truncate_a_long_but_finished_recipe(self):
        """Length alone is not the problem — never closing is."""
        self._prompt("do the big migration")
        for n in range(self.config.max_span_steps + 5):
            self._run(f"psql -c 'migrate step {n}'")
        self._run("pytest -q")
        from skillpp.capture import handle_stop
        result = handle_stop(self.config, {"session_id": "b1"})
        self.assertEqual(result["status"], "created",
                         "a closed span is kept however long it ran")


class TestExplorationTrimming(TempRoot):
    """A span ends at a closing step but has no start marker."""

    def _run(self, cmd, sid="e1", failed=False):
        handle_tool(self.config, {
            "session_id": sid, "cwd": "/p", "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "tool_response": {"exit_code": 1 if failed else 0}})

    def test_leading_search_is_dropped_but_the_fix_is_kept(self):
        from skillpp.capture import handle_prompt, handle_stop
        handle_prompt(self.config, {"session_id": "e1", "cwd": "/p",
                                    "prompt": "why is login slow?"})
        for n in range(8):
            self._run(f"grep -rn handler{n} .")
        self._run("vim auth.py")
        self._run("pytest -q")
        handle_stop(self.config, {"session_id": "e1"})

        entry = list(Ledger(self.config).all())[0]
        self.assertEqual(len(entry.steps), 2, "the fix, not the search for it")
        self.assertIn("vim auth.py",
                      str((entry.steps[0].get("input") or {}).get("command")))

    def test_interleaved_reads_are_kept(self):
        """`check the logs, then restart` is a real two-step recipe."""
        from skillpp.capture import trim_leading_exploration
        steps = [
            {"tool": "Bash", "input": {"command": "kubectl rollout restart deploy/api"}},
            {"tool": "Bash", "input": {"command": "kubectl logs deploy/api"}},
        ]
        kept, cut = trim_leading_exploration(steps)
        self.assertEqual(cut, 0)
        self.assertEqual(len(kept), 2)

    def test_never_trims_a_span_down_to_nothing(self):
        from skillpp.capture import trim_leading_exploration
        steps = [{"tool": "Bash", "input": {"command": f"grep -rn x{n} ."}}
                 for n in range(4)]
        kept, cut = trim_leading_exploration(steps)
        self.assertEqual((len(kept), cut), (4, 0), "all-reads is left intact here")

    def test_exploration_only_span_is_not_banked(self):
        from skillpp.capture import handle_prompt, handle_session_end
        handle_prompt(self.config, {"session_id": "e2", "cwd": "/p",
                                    "prompt": "look into the cache"})
        self._run("cat cache.py", sid="e2")
        self._run("grep -rn evict .", sid="e2")
        result = handle_session_end(self.config, {"session_id": "e2"})
        self.assertEqual(result["status"], "exploration-only")
        self.assertEqual(list(Ledger(self.config).all()), [])

    def test_finished_work_without_a_known_closing_step_still_banks(self):
        """A bespoke rollback is real work even if no regex recognises it."""
        from skillpp.capture import handle_prompt, handle_session_end
        handle_prompt(self.config, {"session_id": "e3", "cwd": "/p",
                                    "prompt": "roll back the bad migration"})
        for cmd in ("psql -c 'begin'", "./rollback.sh", "psql -c 'commit'"):
            self._run(cmd, sid="e3")
        result = handle_session_end(self.config, {"session_id": "e3"})
        self.assertEqual(result["status"], "created")


class TestCrisp(unittest.TestCase):
    def test_drops_plumbing_but_keeps_the_command(self):
        from skillpp.normalize import crisp
        self.assertEqual(crisp("python3 -m unittest discover -s tests 2>&1 | tail -4"),
                         "python3 -m unittest discover -s tests")
        self.assertEqual(crisp("git show abc --stat; echo done"), "git show abc --stat")
        self.assertEqual(crisp("python3 - <<'EOF'\nimport sys\nEOF"),
                         "python3 - [script]")

    def test_keeps_what_normalize_command_would_destroy(self):
        """crisp() must not hide the fact that tests ran."""
        from skillpp.normalize import crisp, normalize_command
        cmd = "python3 -m unittest discover -s tests"
        self.assertEqual(normalize_command(cmd), "python3")
        self.assertIn("unittest", crisp(cmd))

    def test_narration_is_recognised(self):
        from skillpp.normalize import is_narration
        self.assertTrue(is_narration('echo "=== now the tests ==="'))
        self.assertFalse(is_narration("pytest -q"))
        self.assertFalse(is_narration("git commit -m 'echo'"))

    def test_narration_never_reaches_the_buffer(self):
        cfg = Config(tempfile.mkdtemp()); cfg.ensure_dirs()
        from skillpp.capture import _load_session
        for cmd in ('echo "=== step one ==="', "pytest -q"):
            handle_tool(cfg, {"session_id": "n1", "cwd": "/p", "tool_name": "Bash",
                              "tool_input": {"command": cmd},
                              "tool_response": {"exit_code": 0}})
        steps = _load_session(cfg, "n1")["steps"]
        self.assertEqual([s["input"]["command"] for s in steps], ["pytest -q"])


class TestWebUI(TempRoot):
    """The web layer writes real files, so the guards matter more than the HTML."""

    def _skill(self, name, desc="Do a thing.", body="body\n"):
        d = self.root / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        p = d / "SKILL.md"
        p.write_text(f'---\nname: {name}\ndescription: "{desc}"\n---\n\n{body}',
                     encoding="utf-8")
        return p

    def test_rejects_path_traversal_and_unknown_names(self):
        from skillpp.web import _skill_path
        self._skill("real-skill")
        skills = self.root / "skills"
        self.assertIsNotNone(_skill_path(skills, self.config, "real-skill"))
        for bad in ("../../etc/passwd", "a/../../b", "", ".", "..",
                    "/etc/passwd", "nope"):
            self.assertIsNone(_skill_path(skills, self.config, bad),
                              f"{bad!r} must not resolve")

    def test_save_validates_before_overwriting(self):
        from skillpp.web import save_skill
        path = self._skill("guarded")
        original = path.read_text()

        bad = '---\nname: guarded\ndescription: "' + ("x" * 250) + '"\n---\nbody\n'
        result = save_skill(path, bad)
        self.assertFalse(result["ok"])
        self.assertTrue(any("description" in p for p in result["problems"]))
        self.assertEqual(path.read_text(), original,
                         "a rejected save must not touch the file")

    def test_save_keeps_a_backup(self):
        from skillpp.web import save_skill
        path = self._skill("backed-up")
        good = '---\nname: backed-up\ndescription: "Still fine."\n---\n\nnew body\n'
        self.assertTrue(save_skill(path, good)["ok"])
        self.assertIn("new body", path.read_text())
        self.assertTrue(list(path.parent.glob("SKILL.md.bak-*")),
                        "the previous version is recoverable")

    def test_state_payload_covers_every_pane(self):
        from skillpp.web import collect_state
        from skillpp.capture import fold_dictation
        self._skill("listed")
        fold_dictation(self.config, "Whenever I ship, tag and push it.")
        state = collect_state(self.config, self.root / "skills")
        self.assertEqual([s["name"] for s in state["skills"]], ["listed"])
        self.assertEqual(len(state["candidates"]), 1)
        self.assertIn("ignored", state)
        self.assertIn("drift", state)

    def test_entry_actions_round_trip(self):
        from skillpp.web import apply_entry_action
        from skillpp.capture import fold_dictation
        entry_id = fold_dictation(self.config, "Tag the release and push it.")["id"]

        self.assertEqual(apply_entry_action(self.config, entry_id, "ignore"),
                         {"ok": True, "status": "ignored"})
        self.assertEqual(Ledger(self.config).get(entry_id).status, "ignored")
        self.assertEqual(apply_entry_action(self.config, entry_id, "reopen"),
                         {"ok": True, "status": "candidate"})
        self.assertFalse(apply_entry_action(self.config, entry_id, "rm -rf")["ok"])
        self.assertFalse(apply_entry_action(self.config, "nope", "ignore")["ok"])

    def test_archive_keeps_the_file_and_parks_the_workflow(self):
        from skillpp.web import apply_skill_action
        from skillpp.capture import fold_dictation
        path = self._skill("archivable")
        entry_id = fold_dictation(self.config, "Tag the release and push it.")["id"]
        ledger = Ledger(self.config)
        entry = ledger.get(entry_id)
        entry.status = "promoted"; entry.skill_path = str(path); ledger.save(entry)

        result = apply_skill_action(self.config, self.root / "skills",
                                    "archivable", "archive")
        self.assertTrue(result["ok"])
        self.assertFalse(path.exists(), "moved out of the active directory")
        self.assertTrue((self.config.archive_dir / "archivable" / "SKILL.md").exists(),
                        "archive keeps the file")
        self.assertEqual(ledger.get(entry_id).status, "ignored",
                         "the workflow is parked, not left to re-propose")

    def test_delete_removes_the_file_and_parks_the_workflow(self):
        from skillpp.web import apply_skill_action
        from skillpp.capture import fold_dictation
        path = self._skill("disposable")
        entry_id = fold_dictation(self.config, "Roll back the bad migration.")["id"]
        ledger = Ledger(self.config)
        entry = ledger.get(entry_id)
        entry.status = "promoted"; entry.skill_path = str(path); ledger.save(entry)

        result = apply_skill_action(self.config, self.root / "skills",
                                    "disposable", "delete")
        self.assertTrue(result["ok"])
        self.assertFalse(path.parent.exists(), "the folder is gone")
        parked = ledger.get(entry_id)
        self.assertEqual(parked.status, "ignored")
        self.assertEqual(parked.skill_path, "", "stale path cleared")

    def test_skill_actions_are_guarded(self):
        from skillpp.web import apply_skill_action
        skills = self.root / "skills"
        self._skill("safe")
        self.assertFalse(apply_skill_action(self.config, skills, "safe", "nuke")["ok"])
        self.assertFalse(
            apply_skill_action(self.config, skills, "../../etc", "delete")["ok"])
        self.assertFalse(
            apply_skill_action(self.config, skills, "missing", "delete")["ok"])
        self.assertTrue((skills / "safe" / "SKILL.md").exists(),
                        "a rejected action touches nothing")

    def test_server_binds_loopback_only(self):
        from skillpp.web import serve
        httpd = serve(self.config, self.root / "skills", port=0, open_browser=False)
        try:
            self.assertEqual(httpd.server_address[0], "127.0.0.1")
        finally:
            httpd.server_close()


class TestHookRobustness(TempRoot):
    def test_hook_never_raises_on_garbage(self):
        from skillpp.cli import main
        import io
        bad_payloads = ['not json', '{}', '{"tool_name": 123}', '[]', '']
        for payload in bad_payloads:
            stdin = sys.stdin
            sys.stdin = io.StringIO(payload)
            try:
                code = main(["--root", str(self.config.root), "hook",
                             "--event", "PostToolUse"])
            finally:
                sys.stdin = stdin
            self.assertEqual(code, 0, f"hook must exit 0 for payload {payload!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
