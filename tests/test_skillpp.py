"""Unit tests. Stdlib only: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillpp.capture import fold_session, handle_prompt, handle_tool, handle_session_end
from skillpp.config import Config
from skillpp.ledger import Entry, Ledger, make_id
from skillpp import memory
from skillpp.lifecycle import check_staleness, parse_frontmatter, scan
from skillpp.normalize import normalize_command, parameterize, signature
from skillpp.recurrence import find_match, similarity
from skillpp.sanitize import scrub
from skillpp.signals import detect, effects
from skillpp.summary import check_dependencies, scaffold_skill
from skillpp.extract import extract
from skillpp.identity import NoConversationError, conversation_id
from skillpp.memory import (CANDIDATE, PROMOTED, Candidate,
                            add_entry, add_occurrence, find, load,
                            record_decision)
from skillpp.memory import render as render_memory
from skillpp.prepare import (MIN_NEW_MESSAGES, TranscriptNotFound,
                                     commit, prepare, project_slug, record,
                                     render, resolve)
from skillpp.transcript import Message, TranscriptFormatError, read


def bash(command: str, failed: bool = False) -> dict:
    return {"tool": "Bash", "input": {"command": command}, "failed": failed}


# --------------------------------------------------------------------------
# transcript fixtures — build records in the shape Claude Code writes them
# --------------------------------------------------------------------------

def rec(uuid: str, role: str = "user", content=None, sidechain: bool = False,
        ts: str = "2026-08-17T09:00:00.000Z", rtype: str | None = None) -> dict:
    """One conversation record."""
    return {
        "type": rtype or role,
        "uuid": uuid,
        "isSidechain": sidechain,
        "timestamp": ts,
        "message": {"role": role, "content": content if content is not None else "hi"},
    }


def tool_use(name: str, **inp) -> dict:
    return {"type": "tool_use", "id": f"tu_{name}", "name": name, "input": inp}


def tool_result(text: str, is_error: bool = False, tid: str = "tu_Bash") -> dict:
    block = {"type": "tool_result", "tool_use_id": tid, "content": text}
    if is_error:
        block["is_error"] = True
    return block


def write_transcript(root: Path, *records: dict, name: str = "t.jsonl",
                     trailing: str = "") -> Path:
    path = root / name
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records) + trailing, encoding="utf-8")
    return path


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

    def test_scan_reports_which_tier_each_skill_is_in(self):
        # Usage used to be asserted here too. It was only ever written by a
        # PostToolUse hook nobody installed, so in real use every skill read
        # "never used" -- and an invocation is already recorded in the
        # transcript, where it can be counted without a second account of it.
        hot = self.root / "skills"
        self._write_skill(hot, "alpha")
        self._write_skill(self.config.cold_dir, "beta")
        skills = {s.name: s for s in scan(hot, self.config, self.root)}
        self.assertEqual(skills["alpha"].tier, "hot")
        self.assertEqual(skills["beta"].tier, "cold")

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
        """Append to the event we install; leave every other event alone.

        `PostToolUse` here is somebody else's hook, not a stale one of ours —
        skillpp stopped installing that event once detection began reading the
        transcript directly, so touching it would be destroying a stranger's
        configuration.
        """
        from skillpp.install import plan_settings
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "model": "opus",
                "hooks": {
                    "PostToolUse": [{"matcher": "Bash", "hooks": [
                        {"type": "command", "command": "someone-elses.sh"}]}],
                    "SessionEnd": [{"matcher": "*", "hooks": [
                        {"type": "command", "command": "existing.sh"}]}]}}))
            merged, changes = plan_settings(path)
            on_disk = json.loads(path.read_text())
        self.assertEqual(merged["model"], "opus", "unrelated settings survive")
        self.assertIn("existing.sh", json.dumps(merged["hooks"]["SessionEnd"]))
        self.assertEqual(len(merged["hooks"]["SessionEnd"]), 2, "appends, not replaces")
        self.assertEqual(merged["hooks"]["PostToolUse"],
                         [{"matcher": "Bash", "hooks": [
                             {"type": "command", "command": "someone-elses.sh"}]}],
                         "an event skillpp does not install is left untouched")
        self.assertEqual(len(on_disk["hooks"]["SessionEnd"]), 1, "planning must not write")

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


class TestTranscript(TempRoot):
    """The airlock: raw undocumented JSONL in, a small stable shape out."""

    def test_keeps_conversation_records_in_file_order(self):
        path = write_transcript(
            self.root,
            rec("u1", "user", "first"),
            rec("a1", "assistant", [{"type": "text", "text": "reply"}]),
            rec("u2", "user", "second"),
        )
        self.assertEqual([m.uuid for m in read(path)], ["u1", "a1", "u2"])

    def test_drops_bookkeeping_records(self):
        path = write_transcript(
            self.root,
            rec("u1"),
            {"type": "attachment", "uuid": "x1"},
            {"type": "system", "subtype": "turn_duration", "durationMs": 12},
            {"type": "file-history-snapshot", "uuid": "x2"},
            {"type": "ai-title", "title": "something"},
        )
        self.assertEqual([m.uuid for m in read(path)], ["u1"])

    def test_drops_sidechain_traffic(self):
        # Subagent chatter is not the developer's conversation.
        path = write_transcript(
            self.root,
            rec("u1"),
            rec("s1", "assistant", "subagent", sidechain=True),
            rec("a1", "assistant", "real"),
        )
        self.assertEqual([m.uuid for m in read(path)], ["u1", "a1"])

    def test_drops_records_without_a_uuid(self):
        # The uuid is the identity everything downstream keys on.
        path = write_transcript(self.root, rec("u1"), {"type": "user", "message": {}})
        self.assertEqual([m.uuid for m in read(path)], ["u1"])

    def test_tolerates_a_torn_final_line(self):
        # The file is written asynchronously and may be mid-flush when read.
        path = write_transcript(
            self.root, rec("u1"), trailing='{"type": "user", "uu')
        self.assertEqual([m.uuid for m in read(path)], ["u1"])

    def test_empty_file_is_not_an_error(self):
        path = write_transcript(self.root)
        self.assertEqual(read(path), [])

    def test_records_but_no_messages_raises(self):
        # Verified against every transcript on disk: a file with records always
        # has messages. So this combination means the format moved, and
        # returning [] would silently summarise nothing.
        path = write_transcript(
            self.root,
            {"type": "attachment", "uuid": "x1"},
            {"type": "ai-title", "title": "t"},
        )
        with self.assertRaises(TranscriptFormatError):
            read(path)

    def test_normalises_string_and_block_content_alike(self):
        path = write_transcript(
            self.root,
            rec("u1", "user", "a bare string"),
            rec("a1", "assistant", [{"type": "text", "text": "a block"}]),
        )
        self.assertEqual([m.text for m in read(path)], ["a bare string", "a block"])

    def test_normalises_every_block_kind(self):
        path = write_transcript(self.root, rec("a1", "assistant", [
            {"type": "thinking", "thinking": "pondering"},
            {"type": "text", "text": "saying"},
            tool_use("Bash", command="ls -la"),
            {"type": "image", "source": {}},
        ]))
        blocks = read(path)[0].blocks
        self.assertEqual([b.kind for b in blocks],
                         ["thinking", "text", "tool_use", "other"])
        self.assertEqual(blocks[0].text, "pondering")
        self.assertEqual(blocks[2].name, "Bash")
        self.assertEqual(blocks[2].tool_input, {"command": "ls -la"})

    def test_tool_result_carries_its_error_flag(self):
        # Whether a command failed is the signal the summary's judgement is
        # built from; a missing is_error means success, not unknown.
        path = write_transcript(self.root, rec("u1", "user", [
            tool_result("pytest not found", is_error=True),
            tool_result("71 passed"),
        ]))
        blocks = read(path)[0].blocks
        self.assertEqual([b.is_error for b in blocks], [True, False])
        self.assertEqual(blocks[0].text, "pytest not found")

    def test_flattens_a_block_shaped_tool_result(self):
        path = write_transcript(self.root, rec("u1", "user", [{
            "type": "tool_result", "tool_use_id": "tu_Read",
            "content": [{"type": "text", "text": "line one"},
                        {"type": "text", "text": "line two"}],
        }]))
        self.assertEqual(read(path)[0].blocks[0].text, "line one\nline two")

    def test_text_property_ignores_tool_blocks(self):
        path = write_transcript(self.root, rec("a1", "assistant", [
            {"type": "text", "text": "explaining"},
            tool_use("Read", file_path="/tmp/x"),
        ]))
        self.assertEqual(read(path)[0].text, "explaining")

    def test_reads_a_real_transcript_if_one_is_present(self):
        """Guards the fixtures against drifting from the real format."""
        root = Path.home() / ".claude" / "projects"
        real = sorted(root.glob("*/*.jsonl")) if root.exists() else []
        real = [p for p in real if p.stat().st_size > 1000]
        if not real:
            self.skipTest("no local transcripts to check against")
        messages = read(real[0])
        self.assertTrue(messages, "a non-empty transcript yielded no messages")
        self.assertTrue(all(m.uuid and m.timestamp for m in messages))
        self.assertTrue(any(b.kind == "tool_use" for m in messages for b in m.blocks))


class TestIdentity(TempRoot):
    """Every case here mirrors a snapshot relationship seen in real data."""

    def ident(self, *records: dict, name: str = "t.jsonl") -> str:
        return conversation_id(read(write_transcript(self.root, *records, name=name)))

    def test_same_conversation_across_a_resume(self):
        # A resume copies the whole history forward into a new file.
        first = self.ident(rec("u1"), rec("a1", "assistant"), name="a1.jsonl")
        resumed = self.ident(rec("u1"), rec("a1", "assistant"),
                             rec("u2"), rec("a2", "assistant"), name="a2.jsonl")
        self.assertEqual(first, resumed)

    def test_survives_a_rewritten_tail(self):
        # 25 of 44 real snapshot pairs had their last messages rewritten.
        before = self.ident(rec("u1"), rec("old1"), rec("old2"), name="b.jsonl")
        after = self.ident(rec("u1"), rec("new1"), rec("new2"), rec("new3"),
                           name="a.jsonl")
        self.assertEqual(before, after)

    def test_survives_an_early_rewind(self):
        # A real conversation diverged at message index 2. Any prefix longer
        # than one message would file these as two separate conversations.
        branch_a = self.ident(rec("u1"), rec("a1"), rec("x1"), name="ba.jsonl")
        branch_b = self.ident(rec("u1"), rec("a1"), rec("y1"), rec("y2"),
                              name="bb.jsonl")
        self.assertEqual(branch_a, branch_b)

    def test_diverges_immediately_still_matches(self):
        # The earliest divergence possible: only the first message is shared.
        self.assertEqual(self.ident(rec("u1"), rec("p"), name="p.jsonl"),
                         self.ident(rec("u1"), rec("q"), name="q.jsonl"))

    def test_unrelated_conversations_differ(self):
        self.assertNotEqual(self.ident(rec("u1"), name="one.jsonl"),
                            self.ident(rec("z9"), name="two.jsonl"))

    def test_is_deterministic(self):
        records = (rec("u1"), rec("a1", "assistant"))
        self.assertEqual(self.ident(*records, name="x.jsonl"),
                         self.ident(*records, name="y.jsonl"))

    def test_ignores_sidechains_when_they_come_first(self):
        # A subagent record before the first real message must not become the
        # identity, or the id changes depending on subagent activity.
        with_side = self.ident(rec("s0", "assistant", sidechain=True), rec("u1"),
                               name="s.jsonl")
        without = self.ident(rec("u1"), name="n.jsonl")
        self.assertEqual(with_side, without)

    def test_is_filename_safe_and_short(self):
        ident = self.ident(rec("7f3a9c2e-1234-5678-9abc-def012345678"))
        self.assertEqual(ident, "7f3a9c2e12345678")
        self.assertNotIn("-", ident)
        self.assertEqual(len(ident), 16)

    def test_empty_transcript_raises(self):
        with self.assertRaises(NoConversationError):
            conversation_id([])

    def test_groups_real_transcripts_without_splitting(self):
        """The property the whole module exists for, checked on real data."""
        root = Path.home() / ".claude" / "projects"
        files = [p for p in sorted(root.glob("*/*.jsonl"))
                 if p.stat().st_size > 1000] if root.exists() else []
        if len(files) < 5:
            self.skipTest("not enough local transcripts to check grouping")
        groups: dict[str, int] = {}
        for path in files:
            messages = read(path)
            if messages:
                ident = conversation_id(messages)
                groups[ident] = groups.get(ident, 0) + 1
        self.assertLess(len(groups), len(files),
                        "no transcripts grouped — resumes are not being detected")


class TestExtract(TempRoot):
    """Each case pins a decision that was made by measurement."""

    def out(self, *records: dict) -> str:
        return extract(read(write_transcript(self.root, *records)))

    def test_keeps_prompts_and_tool_calls_with_arguments(self):
        text = self.out(
            rec("u1", "user", "run the test suite"),
            rec("a1", "assistant", [tool_use("Bash", command="pytest tests/")]),
        )
        self.assertIn("> run the test suite", text)
        self.assertIn("$ Bash pytest tests/", text)

    def test_keeps_the_outcome_of_a_command(self):
        # The judgement in a summary is built from outcomes, not commands.
        text = self.out(
            rec("u1", "user", "run them"),
            rec("a1", "assistant", [tool_use("Bash", command="pytest")]),
            rec("u2", "user", [tool_result("71 passed in 0.17s")]),
        )
        self.assertIn("71 passed", text)

    def test_drops_the_outcome_of_a_read(self):
        # The last lines of a file read say nothing about what happened.
        text = self.out(
            rec("a1", "assistant", [tool_use("Read", file_path="/tmp/x.py")]),
            rec("u1", "user", [tool_result("341\n342\n343", tid="tu_Read")]),
        )
        self.assertIn("$ Read /tmp/x.py", text)
        self.assertNotIn("343", text)

    def test_keeps_a_failure_whatever_the_tool(self):
        text = self.out(
            rec("a1", "assistant", [tool_use("Read", file_path="/tmp/gone")]),
            rec("u1", "user", [tool_result("File does not exist",
                                           is_error=True, tid="tu_Read")]),
        )
        self.assertIn("File does not exist", text)

    def test_marks_failures_differently_from_successes(self):
        text = self.out(
            rec("a1", "assistant", [tool_use("Bash", command="which pipx")]),
            rec("u1", "user", [tool_result("pipx not found", is_error=True)]),
        )
        self.assertIn("! pipx not found", text)

    def test_drops_thinking(self):
        text = self.out(rec("a1", "assistant", [
            {"type": "thinking", "thinking": "a long internal monologue"},
            {"type": "text", "text": "the answer"},
        ]))
        self.assertNotIn("monologue", text)

    def test_drops_an_expanded_command_definition(self):
        # /log-session alone contributed ~5KB to an 8KB extract.
        body = ("# Skill Plus Plus — log a session\n\n"
                + "Read the transcript from disk rather than from context.\n" * 8
                + "\n## 1. Resolve\n\ndetail\n\n## 2. Read it\n\ndetail\n")
        text = self.out(rec("u1", "user", body), rec("u2", "user", "actually do it"))
        self.assertNotIn("Resolve", text)
        self.assertIn("actually do it", text)

    def test_strips_slash_command_plumbing(self):
        text = self.out(rec("u1", "user",
                            "<command-name>/exit</command-name>real question"))
        self.assertNotIn("command-name", text)
        self.assertIn("real question", text)

    def test_keeps_the_question_behind_a_short_reply(self):
        # "3" is meaningless without the options it answered.
        text = self.out(
            rec("a1", "assistant", [{"type": "text", "text":
                "Options:\n1. pip install\n2. uv run --with\n3. pipx run"}]),
            rec("u1", "user", "3"),
        )
        self.assertIn("pipx run", text)
        self.assertIn("> 3", text)

    def test_keeps_what_the_agent_said(self):
        # Some practices live entirely in the dialogue: proposing a plan and
        # inviting challenge, or asking for scope before touching code. None
        # of that appears in a tool call.
        text = self.out(rec("a1", "assistant", [{"type": "text", "text":
            "Before I change anything — is this scoped to the token check, "
            "or the whole auth flow?"}]))
        self.assertIn("scoped to the token check", text)

    def test_caps_a_very_long_assistant_turn(self):
        text = self.out(rec("a1", "assistant",
                            [{"type": "text", "text": "y" * 3000}]))
        self.assertIn("…", text)
        self.assertLess(len(text), 1000)

    def test_keeps_the_questions_and_options_asked(self):
        text = self.out(rec("a1", "assistant", [tool_use(
            "AskUserQuestion",
            questions=[{"question": "Should I touch the tests?",
                        "options": [{"label": "Yes, update them"},
                                    {"label": "Leave them alone"}]}])]))
        self.assertIn("Should I touch the tests?", text)
        self.assertIn("Leave them alone", text)

    def test_keeps_a_proposed_plan(self):
        text = self.out(rec("a1", "assistant", [tool_use(
            "ExitPlanMode", plan="# Plan\n1. Read the config\n2. Ask before writing")]))
        self.assertIn("Ask before writing", text)

    def test_drops_sidechain_and_empty_turns(self):
        text = self.out(
            rec("u1", "user", "go"),
            rec("s1", "assistant", "subagent chatter", sidechain=True),
        )
        self.assertNotIn("subagent", text)

    def test_truncates_a_huge_argument_at_both_ends(self):
        # A truncated heredoc loses what it was writing, so keep the close too.
        text = self.out(rec("a1", "assistant",
                            [tool_use("Bash", command="A" * 3000 + "ZZZEND")]))
        self.assertIn("…", text)
        self.assertIn("ZZZEND", text)
        self.assertLess(len(text), 2000)

    def test_keeps_the_command_but_not_the_file_it_wrote(self):
        # The argument of a Bash call is the step. The argument of a Write is
        # the product — the step is "wrote the file".
        text = self.out(rec("a1", "assistant", [
            tool_use("Bash", command="gh pr create --base main --title 'x'"),
            tool_use("Write", file_path="docs/report.md", content="Q" * 4000),
        ]))
        self.assertIn("gh pr create --base main", text)
        self.assertIn("docs/report.md", text)
        self.assertLess(text.count("Q"), 300)

    def test_shortens_a_pasted_log_but_keeps_both_ends(self):
        # Developers paste stack traces into prompts. The ask is at the ends;
        # the paste in the middle is the same noise stripped everywhere else.
        prompt = "any hints here?\n" + ("TRACEBACK LINE\n" * 400) + "\nwhat do I do?"
        text = self.out(rec("u1", "user", prompt))
        self.assertIn("any hints here?", text)
        self.assertIn("what do I do?", text)
        self.assertIn("characters pasted", text)
        self.assertLess(len(text), len(prompt) / 4)

    def test_leaves_an_ordinary_prompt_untouched(self):
        prompt = "refactor the ledger module so entries load lazily"
        self.assertIn(prompt, self.out(rec("u1", "user", prompt)))

    def test_compresses_a_real_transcript_substantially(self):
        """The property the module exists for, on real data."""
        root = Path.home() / ".claude" / "projects"
        files = [p for p in sorted(root.glob("*/*.jsonl"))
                 if p.stat().st_size > 200_000] if root.exists() else []
        if not files:
            self.skipTest("no large local transcript to compress")
        raw = files[0].stat().st_size
        text = extract(read(files[0]))
        self.assertTrue(text, "extraction produced nothing")
        self.assertLess(len(text) * 10, raw, "expected at least 10x compression")


class TestPrepare(TempRoot):
    """The bookkeeping whose failures are all silent."""

    def session(self, *records: dict, name: str = "s.jsonl") -> Path:
        return write_transcript(self.root, *records, name=name)

    def many(self, n: int, prefix: str = "m", start: int = 0) -> list:
        return [rec(f"{prefix}{i}", ts=f"2026-08-17T09:{i:02d}:00.000Z")
                for i in range(start, start + n)]

    def prep(self, path: Path):
        return prepare(str(path), config=self.config)

    # -- resolving -------------------------------------------------------

    def test_resolves_an_explicit_path(self):
        path = self.session(rec("u1"))
        self.assertEqual(self.prep(path).transcript, path)

    def test_missing_transcript_raises(self):
        with self.assertRaises(TranscriptNotFound):
            prepare("no-such-session-anywhere")

    def test_slug_replaces_dots_and_underscores_too(self):
        # Replacing only the slashes yields a path that does not exist.
        self.assertEqual(project_slug("/a/b_c/d.e"), "-a-b-c-d-e")

    # -- first run -------------------------------------------------------

    def test_first_run_offers_everything(self):
        prepared = self.prep(self.session(*self.many(30)))
        self.assertEqual(len(prepared.new_messages), 30)
        self.assertIn("Nothing has been recorded yet", render(prepared))

    def test_every_session_writes_into_one_store(self):
        # Counts only mean anything when every session pools into the same
        # place; a store per conversation leaves everything at one occurrence.
        prepared = self.prep(self.session(*self.many(30)))
        self.assertEqual(prepared.config.patterns_dir.name, "patterns")
        self.assertEqual(prepared.review_path.name, f"{prepared.session}.md")

    # -- the floor -------------------------------------------------------

    def test_below_the_floor_says_stop(self):
        prepared = self.prep(self.session(*self.many(MIN_NEW_MESSAGES - 1)))
        self.assertFalse(prepared.has_enough)
        text = render(prepared)
        self.assertIn("Stop here and write nothing", text)
        self.assertIn("next batch", text)

    def test_at_the_floor_proceeds(self):
        self.assertTrue(self.prep(self.session(*self.many(MIN_NEW_MESSAGES))).has_enough)

    # -- the watermark ---------------------------------------------------

    def review(self, prepared, *candidates):
        """Record candidates, then declare the session reviewed."""
        for name, body in candidates:
            record(prepared, name=name, body=body)
        return commit(prepared)

    def test_second_run_offers_only_what_is_new(self):
        first = self.prep(self.session(*self.many(30), name="a.jsonl"))
        self.review(first, ("filing-a-ticket", "Check the spec page first."))
        second = prepare(str(self.session(*self.many(45), name="b.jsonl")),
                         config=self.config)
        self.assertEqual(len(second.new_messages), 15)
        self.assertIn("filing-a-ticket", second.patterns)

    def test_rerunning_the_same_transcript_offers_nothing(self):
        path = self.session(*self.many(30))
        self.review(self.prep(path), ("x", "body"))
        again = self.prep(path)
        self.assertEqual(again.new_messages, [])
        self.assertIn("Stop here", render(again))

    def test_falls_back_to_timestamp_when_the_marked_message_is_gone(self):
        # Interrupt a tool call and the turn is regenerated, so the message
        # the watermark names stops existing. Timestamps survive that.
        self.review(self.prep(self.session(*self.many(30), name="a.jsonl")))
        rewritten = self.session(*self.many(29), *[
            rec("regen", ts="2026-08-17T09:35:00.000Z"),
            rec("new1", ts="2026-08-17T09:36:00.000Z"),
        ], name="b.jsonl")
        second = prepare(str(rewritten), config=self.config)
        self.assertEqual([m.uuid for m in second.new_messages], ["regen", "new1"])

    def test_a_regenerated_turn_straddling_the_mark_is_split(self):
        """A known limitation, pinned so it is not discovered by surprise.

        The fallback compares timestamps, so a turn regenerated partly before
        and partly after the marked moment comes back only in part. Nothing
        already reviewed is repeated — the guarantee that matters — but a
        sliver of the rewritten turn is missed.
        """
        self.review(self.prep(self.session(*self.many(30), name="a.jsonl")))
        rewritten = self.session(*self.many(28), *[
            rec("before", ts="2026-08-17T09:28:30.000Z"),  # predates the mark
            rec("after", ts="2026-08-17T09:29:30.000Z"),
        ], name="b.jsonl")
        second = prepare(str(rewritten), config=self.config)
        self.assertEqual([m.uuid for m in second.new_messages], ["after"])

    def test_unresolvable_watermark_raises_rather_than_guessing(self):
        # Guessing "it is all new" silently doubles every count in the file.
        self.review(self.prep(self.session(*self.many(30), name="a.jsonl")))
        stripped = write_transcript(
            self.root,
            *[{"type": "user", "uuid": ("m0" if i == 0 else f"z{i}"),
               "message": {"role": "user", "content": "hi"}} for i in range(30)],
            name="c.jsonl")
        with self.assertRaises(ValueError):
            prepare(str(stripped), config=self.config)

    # -- recording and committing ----------------------------------------

    def test_commit_writes_the_bookmark_into_the_review(self):
        prepared = self.prep(self.session(*self.many(30)))
        review = self.review(prepared, ("filing-a-ticket", "Check the spec.")).read_text()
        self.assertIn(f"conversation: {prepared.conversation}", review)
        self.assertIn(f"last_message: {prepared.new_messages[-1].uuid}", review)
        self.assertIn("last_timestamp: 2026-08-17T09:29", review)
        self.assertIn("## filing-a-ticket", review)
        # The candidate itself lives in its own entry file, not the review.
        entry = self.config.patterns_dir / "filing-a-ticket.md"
        self.assertIn("Check the spec.", entry.read_text())

    def test_a_barren_session_still_leaves_a_bookmark(self):
        # Without one its messages are read again forever, and "reviewed,
        # found nothing" is the only evidence the filter is not too strict.
        prepared = self.prep(self.session(*self.many(30)))
        commit(prepared)
        review = prepared.review_path.read_text()
        self.assertIn("No candidate found", review)
        self.assertIn(f"last_message: {prepared.new_messages[-1].uuid}", review)
        self.assertEqual(len(self.prep(self.session(*self.many(30))).new_messages), 0)

    def test_two_sessions_sharing_a_prefix_count_separately(self):
        # A truncated session id silently merged these: the second was read as
        # already present, so the count stopped rising with no error.
        first = self.prep(self.session(*self.many(30), name="recurrence-a.jsonl"))
        record(first, name="filing", body="one")
        commit(first)
        second = prepare(str(self.session(*self.many(60), name="recurrence-b.jsonl")),
                         config=self.config)
        record(second, name="filing", body="one and two", matches="filing")
        entries = load(self.config)
        self.assertEqual(entries[0].count, 2)
        self.assertEqual(entries[0].sessions,
                         ["recurrence-a", "recurrence-b"])

    def test_recording_alone_does_not_move_the_watermark(self):
        # A session may yield three candidates or none; the mark moves once,
        # and only after the recording succeeded.
        path = self.session(*self.many(30))
        record(self.prep(path), name="x", body="body")
        self.assertEqual(len(self.prep(path).new_messages), 30)

    def test_commit_leaves_no_temporary_file(self):
        self.review(self.prep(self.session(*self.many(30))), ("x", "body"))
        self.assertEqual(list(self.config.reviews_dir.glob("*.tmp")), [])

    def test_a_later_session_cannot_drop_an_earlier_candidate(self):
        """The reason the model no longer rewrites the whole document.

        It hands over one candidate at a time, so it has no way to touch the
        others — an entry cannot be lost by being forgotten.
        """
        first = self.prep(self.session(*self.many(30), name="a.jsonl"))
        self.review(first, ("filing-a-ticket", "one"), ("merging", "two"))
        second = prepare(str(self.session(*self.many(60), name="b.jsonl")),
                         config=self.config)
        self.review(second, ("something-else", "three"))
        for name in ("filing-a-ticket", "merging", "something-else"):
            self.assertTrue((self.config.patterns_dir / f"{name}.md").exists(),
                            f"{name} lost")

    # -- the review record -----------------------------------------------

    def review_file(self, prepared):
        return self.config.reviews_dir / f"{prepared.transcript.stem}.md"

    def test_each_review_keeps_what_it_proposed(self):
        # The memory holds only the current best description of a procedure.
        # This holds what each session actually said, so a revised prompt can
        # re-merge from judged proposals rather than re-reading transcripts.
        prepared = self.prep(self.session(*self.many(30), name="a.jsonl"))
        record(prepared, name="filing-a-ticket", body="Check the spec page.")
        text = self.review_file(prepared).read_text()
        self.assertIn(f"session: {prepared.transcript.stem}", text)
        self.assertIn(f"conversation: {prepared.conversation}", text)
        self.assertIn("## filing-a-ticket", text)
        self.assertIn("Check the spec page.", text)

    def test_a_review_records_every_proposal_it_made(self):
        prepared = self.prep(self.session(*self.many(30), name="a.jsonl"))
        for name in ("first-one", "second-one"):
            record(prepared, name=name, body=f"{name} body")
        text = self.review_file(prepared).read_text()
        self.assertIn("## first-one", text)
        self.assertIn("## second-one", text)
        self.assertEqual(text.count("\n---\n"), 1, "one frontmatter block only")

    def test_a_review_says_what_a_proposal_matched(self):
        """And does not call it a merge.

        It said "merged into" while merging nothing, so a model composed the
        two bodies combined -- as the command file then told it to -- and the
        result was dropped. The wording is the contract with the caller.
        """
        first = self.prep(self.session(*self.many(30), name="a.jsonl"))
        self.review(first, ("merging-a-branch-safely", "one"))
        second = prepare(str(self.session(*self.many(60), name="b.jsonl")),
                         config=self.config)
        record(second, name="merging-and-cleaning-up", body="one and two",
               matches="merging-a-branch-safely")
        review = self.review_file(second).read_text()
        self.assertIn("another sighting of: merging-a-branch-safely", review)
        self.assertNotIn("merged", review)

    def test_a_match_leaves_the_stored_body_alone(self):
        """A match is evidence, not a rewrite.

        Re-serialising a body on every match is what corrupted the store: one
        bad read wrote a truncated body back over a good one. The stored body
        is what the first occurrence taught; a later account of the same
        procedure survives in its own review rather than overwriting it.
        """
        first = self.prep(self.session(*self.many(30), name="a.jsonl"))
        self.review(first, ("filing", "the first account"))
        entry = self.config.patterns_dir / "filing.md"
        before = entry.read_bytes()

        second = prepare(str(self.session(*self.many(60), name="b.jsonl")),
                         config=self.config)
        record(second, name="filing", body="a later account", matches="filing")

        self.assertEqual(entry.read_bytes(), before, "entry file was rewritten")
        self.assertEqual(load(self.config)[0].count, 2)
        self.assertIn("a later account",
                      self.review_file(second).read_text())

    def test_render_carries_the_store_and_the_new_work(self):
        first = self.prep(self.session(*self.many(30), name="a.jsonl"))
        self.review(first, ("what-we-learned", "Check the spec page first."))
        text = render(prepare(str(self.session(*self.many(60), name="b.jsonl")),
                              config=self.config))
        self.assertIn("what-we-learned", text)
        self.assertIn("New in this session", text)


class TestSkillsDirRedirect(unittest.TestCase):
    """The one write that leaves the root has to be redirectable too."""

    def test_the_environment_wins_over_both_defaults(self):
        # Without this, an eval pointing SKILLPP_ROOT at a scratch directory
        # still promoted into the developer's real ~/.claude/skills.
        from skillpp.config import default_skills_dir
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.dict(os.environ,
                                          {"SKILLPP_SKILLS_DIR": tmp}):
                self.assertEqual(default_skills_dir(), Path(tmp))

    def test_without_it_the_project_directory_still_wins(self):
        from skillpp.config import default_skills_dir
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / ".claude" / "skills"
            local.mkdir(parents=True)
            with unittest.mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SKILLPP_SKILLS_DIR", None)
                self.assertEqual(default_skills_dir(tmp), local)


class TestRejection(TempRoot):
    """Turned down, and the only thing that brings it back.

    A rejection is not a delete: the entry, its body and its provenance stay,
    and sightings keep accruing. It is also not automatic to undo. Nothing
    reopens itself -- re-proposing what a developer just refused is the fastest
    way to get the tool switched off, so the count that builds up afterwards is
    shown to them rather than acted on.
    """

    def seen(self, name, times):
        memory.add_entry(self.config, name=name, body="Do it.")
        for n in range(times):
            memory.add_occurrence(self.config, name=name, session=f"{name}-{n}")

    def queue(self):
        return [c.name for c in memory.reviewable(memory.load(self.config),
                                                  config=self.config)]

    def reject(self, name):
        entry = memory.find(memory.load(self.config), name)
        memory.record_decision(self.config, name=name, action=memory.REJECTED,
                               at_count=entry.count)

    def test_it_leaves_the_queue(self):
        self.seen("noisy", 3)
        self.assertEqual(self.queue(), ["noisy"])
        self.reject("noisy")
        self.assertEqual(self.queue(), [])

    def test_recurrence_does_not_bring_it_back(self):
        """However often it happens again, it is not re-proposed.

        The developer refused it. A count is not an argument they have not
        already heard, and re-asking is what gets the tool switched off.
        """
        self.seen("noisy", 3)
        self.reject("noisy")
        for n in range(20):
            memory.add_occurrence(self.config, name="noisy", session=f"more-{n}")
        self.assertEqual(self.queue(), [])
        self.assertEqual(memory.find(memory.load(self.config), "noisy").count, 23)

    def test_the_sightings_since_are_still_visible(self):
        """Counting continues so a person can see it kept happening."""
        self.seen("noisy", 3)
        self.reject("noisy")
        for n in range(5):
            memory.add_occurrence(self.config, name="noisy", session=f"more-{n}")
        entry = memory.find(memory.load(self.config), "noisy")
        self.assertEqual(entry.decided_at_count, 3)
        self.assertEqual(entry.count - entry.decided_at_count, 5)

    def test_reopening_is_the_way_back(self):
        from skillpp.cli import main
        self.seen("noisy", 3)
        self.reject("noisy")
        self.assertEqual(self.queue(), [])
        main(["--root", str(self.config.root), "reopen-candidate", "noisy"])
        self.assertEqual(self.queue(), ["noisy"])

    def test_reopening_something_never_rejected_changes_nothing(self):
        from skillpp.cli import main
        self.seen("fine", 3)
        main(["--root", str(self.config.root), "reopen-candidate", "fine"])
        entry = memory.find(memory.load(self.config), "fine")
        self.assertEqual(entry.status, memory.CANDIDATE)
        self.assertEqual(self.queue(), ["fine"])

    def test_nothing_is_deleted_by_a_rejection(self):
        self.seen("noisy", 3)
        entry = self.config.patterns_dir / "noisy.md"
        before = entry.read_bytes()
        self.reject("noisy")
        self.assertEqual(entry.read_bytes(), before)
        self.assertEqual(memory.find(memory.load(self.config), "noisy").count, 3)

    def test_a_rejection_is_not_shown_as_a_skill(self):
        """`render` grouped everything non-candidate under "Made into skills".

        A model reading that would have been told a turned-down procedure
        already exists as a skill.
        """
        self.seen("noisy", 1)
        self.reject("noisy")
        out = memory.render(memory.load(self.config))
        made = out.index("## Made into skills")
        self.assertNotIn("noisy", out[made:out.index("## Turned down")])
        self.assertIn("noisy", out[out.index("## Turned down"):])

    def test_a_promotion_still_wins_after_a_rejection(self):
        """Last decision wins, and the two must not be confused for each other."""
        self.seen("noisy", 3)
        self.reject("noisy")
        memory.record_decision(self.config, name="noisy",
                               action=memory.PROMOTED, skill_path="/tmp/x")
        entry = memory.find(memory.load(self.config), "noisy")
        self.assertEqual(entry.status, memory.PROMOTED)
        self.assertEqual(self.queue(), [])


class TestReviewThreshold(TempRoot):
    """What is recorded and what is asked about are different questions."""

    def seen(self, name, times, *, status=None):
        memory.add_entry(self.config, name=name, body="Do the thing.")
        for n in range(times):
            memory.add_occurrence(self.config, name=name, session=f"{name}-{n}")
        if status:
            memory.record_decision(self.config, name=name, action=status)

    def test_one_sighting_is_recorded_but_not_asked_about(self):
        # A procedure that happened once is a log entry. Asking about it spends
        # the only attention the store gets before there is anything to decide.
        self.seen("filing", 1)
        entries = memory.load(self.config)
        self.assertEqual(len(entries), 1, "it should still be stored")
        self.assertEqual(memory.reviewable(entries), [])

    def test_the_threshold_is_three(self):
        self.seen("filing", 2)
        self.assertEqual(memory.reviewable(memory.load(self.config)), [])
        memory.add_occurrence(self.config, name="filing", session="third")
        ready = memory.reviewable(memory.load(self.config))
        self.assertEqual([c.name for c in ready], ["filing"])

    def test_a_promoted_entry_never_returns_to_the_queue(self):
        # Evidence gates the queue; status decides membership. An entry that
        # keeps recurring after promotion is the wanted outcome, not a question.
        self.seen("filing", 5, status=memory.PROMOTED)
        self.assertEqual(memory.reviewable(memory.load(self.config)), [])

    def test_the_threshold_orders_nothing_on_its_own(self):
        # order() is unchanged: the filter is a separate step, so a listing
        # that wants everything still gets everything.
        self.seen("rare", 1)
        self.seen("common", 4)
        self.assertEqual([c.name for c in memory.load(self.config)],
                         ["common", "rare"])


class TestAppendOnlyStore(TempRoot):
    """A body is written once and never rewritten.

    Every test here failed against the single-document store, which
    re-serialised every entry on each write and cut bodies at their first
    ``##`` heading.
    """

    BODY = "Intro line.\n\n## First rule\n\nDo A.\n\n## Second rule\n\nDo B."

    def test_a_body_with_sections_survives_a_round_trip(self):
        memory.add_entry(self.config, name="filing", body=self.BODY)
        memory.add_occurrence(self.config, name="filing", session="s1")
        loaded = memory.find(memory.load(self.config), "filing")
        self.assertEqual(loaded.body, self.BODY)
        self.assertIn("## Second rule", loaded.body)

    def test_an_occurrence_does_not_touch_the_entry_file(self):
        # The invariant, mechanically: a match increments and nothing else.
        path = memory.add_entry(self.config, name="filing", body=self.BODY)
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        memory.add_occurrence(self.config, name="filing", session="s1")
        memory.add_occurrence(self.config, name="filing", session="s2")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_recording_one_entry_leaves_the_others_byte_identical(self):
        # The failure that motivated the rewrite: merging into one entry
        # destroyed an untouched one, because the whole store was rewritten.
        other = memory.add_entry(self.config, name="deploying",
                                 body="Deploy intro.\n\n## Check the lock\n\nDo C.")
        untouched = other.read_bytes()
        memory.add_entry(self.config, name="filing", body=self.BODY)
        memory.add_occurrence(self.config, name="filing", session="s1")
        self.assertEqual(other.read_bytes(), untouched)
        self.assertIn("## Check the lock",
                      memory.find(memory.load(self.config), "deploying").body)

    def test_count_and_dates_are_derived_from_the_log(self):
        memory.add_entry(self.config, name="filing", body=self.BODY)
        memory.add_occurrence(self.config, name="filing", session="s1", today="2026-01-02")
        memory.add_occurrence(self.config, name="filing", session="s2", today="2026-03-04")
        entry = memory.find(memory.load(self.config), "filing")
        self.assertEqual(entry.count, 2)
        self.assertEqual(entry.sessions, ["s1", "s2"])
        self.assertEqual((entry.first_seen, entry.last_seen), ("2026-01-02", "2026-03-04"))

    def test_status_comes_from_the_last_decision(self):
        memory.add_entry(self.config, name="filing", body=self.BODY)
        memory.add_occurrence(self.config, name="filing", session="s1")
        self.assertEqual(memory.find(memory.load(self.config), "filing").status,
                         memory.CANDIDATE)
        memory.record_decision(self.config, name="filing", action=memory.PROMOTED,
                               skill_path="/tmp/x/SKILL.md", today="2026-05-06")
        entry = memory.find(memory.load(self.config), "filing")
        self.assertEqual(entry.status, memory.PROMOTED)
        self.assertEqual(entry.skill_path, "/tmp/x/SKILL.md")
        self.assertEqual(entry.promoted_on, "2026-05-06")

    def test_an_occurrence_after_promotion_does_not_demote(self):
        # Recurring is evidence the skill earns its place, not a reason to
        # put it back in the queue.
        memory.add_entry(self.config, name="filing", body=self.BODY)
        memory.add_occurrence(self.config, name="filing", session="s1")
        memory.record_decision(self.config, name="filing", action=memory.PROMOTED,
                               skill_path="/tmp/x/SKILL.md")
        memory.add_occurrence(self.config, name="filing", session="s2")
        entry = memory.find(memory.load(self.config), "filing")
        self.assertEqual(entry.status, memory.PROMOTED)
        self.assertEqual(entry.count, 2)

    def test_a_body_may_contain_a_horizontal_rule(self):
        # Frontmatter parsing is anchored to the start of the file, so a
        # `---` in the body is content rather than a delimiter.
        body = "Intro.\n\n## Rule\n\nDo A.\n\n---\n\n## Later rule\n\nDo B."
        memory.add_entry(self.config, name="filing", body=body)
        memory.add_occurrence(self.config, name="filing", session="s1")
        self.assertEqual(memory.find(memory.load(self.config), "filing").body, body)

    def test_entries_order_by_count_candidates_first(self):
        for name, sessions in (("rare", ["s1"]), ("common", ["s1", "s2", "s3"])):
            memory.add_entry(self.config, name=name, body=self.BODY)
            for s in sessions:
                memory.add_occurrence(self.config, name=name, session=s)
        memory.record_decision(self.config, name="common", action=memory.PROMOTED,
                               skill_path="/tmp/c/SKILL.md")
        # Promoted sinks below candidates regardless of its higher count.
        self.assertEqual([e.name for e in memory.load(self.config)], ["rare", "common"])

    def test_the_same_session_twice_counts_once(self):
        # Re-running a review must not inflate the evidence.
        memory.add_entry(self.config, name="filing", body=self.BODY)
        memory.add_occurrence(self.config, name="filing", session="s1")
        memory.add_occurrence(self.config, name="filing", session="s1")
        self.assertEqual(memory.find(memory.load(self.config), "filing").count, 1)

    def test_an_empty_store_loads_to_nothing(self):
        self.assertEqual(memory.load(self.config), [])

    def test_writing_over_an_existing_entry_is_refused(self):
        # The entry file is the one copy of what a procedure is.
        memory.add_entry(self.config, name="filing", body=self.BODY)
        with self.assertRaises(FileExistsError):
            memory.add_entry(self.config, name="filing", body="something else")

    def test_a_log_line_for_an_unknown_name_is_ignored(self):
        # A stale log must not conjure an entry with no body.
        memory.add_occurrence(self.config, name="never-recorded", session="s1")
        self.assertEqual(memory.load(self.config), [])

    def test_a_corrupt_log_line_loses_only_that_line(self):
        memory.add_entry(self.config, name="filing", body=self.BODY)
        memory.add_occurrence(self.config, name="filing", session="s1")
        with self.config.occurrences_file.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        memory.add_occurrence(self.config, name="filing", session="s2")
        self.assertEqual(memory.find(memory.load(self.config), "filing").count, 2)

    def test_render_shows_bodies_in_full(self):
        # What the model is given to judge a match against.
        memory.add_entry(self.config, name="filing", body=self.BODY)
        memory.add_occurrence(self.config, name="filing", session="s1")
        out = memory.render(memory.load(self.config))
        self.assertIn("## Second rule", out)
        self.assertIn("**seen 1×** · s1", out)


class TestTheAcceptPath(TempRoot):
    """What happens after a person answers "make it a skill".

    Everything before this was covered: which candidates the queue offers, and
    that nothing is written before an answer exists. The answer itself was not,
    because it arrives through ``AskUserQuestion`` and an eval cannot press the
    button. So the eval stops at the question and this starts one step later,
    at the command the answer triggers -- which is deterministic, and until now
    was the only part of the whole flow with no test at all.

    The invariant that matters most here: promotion is the one operation that
    writes *outside* the store, and it must still not rewrite anything inside
    it. An accepted candidate keeps its file, byte for byte.
    """

    BODY = ("Read the intake spec first — required fields change.\n\n"
            "## Search before filing\n\n"
            "Search by the stack-trace signature, not the title.\n\n"
            "---\n\n"
            "## Link, do not duplicate\n\n"
            "Attach the thread to the existing issue.")

    def setUp(self) -> None:
        super().setUp()
        self.skills = self.root / "skills"
        self.entry_file = memory.add_entry(
            self.config, name="filing-a-bug", body=self.BODY)
        for n in range(3):
            memory.add_occurrence(self.config, name="filing-a-bug",
                                  session=f"s{n}")

    def promote(self, *extra: str) -> int:
        from skillpp.cli import main
        return main(["--root", str(self.config.root), "promote-candidate",
                     "filing-a-bug", "--skills-dir", str(self.skills), *extra])

    @property
    def skill(self) -> Path:
        return self.skills / "filing-a-bug" / "SKILL.md"

    def _decisions(self) -> list[dict]:
        path = self.config.decisions_file
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    def test_accepting_writes_the_skill_and_logs_one_decision(self):
        self.assertEqual(self.promote(), 0)
        self.assertTrue(self.skill.is_file())
        decisions = self._decisions()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["action"], memory.PROMOTED)
        self.assertEqual(decisions[0]["skill_path"], str(self.skill))

    def test_the_body_survives_promotion_intact(self):
        # The old store cut a body at its first `##`, and a `---` inside one
        # was read as a frontmatter delimiter. A skill written from a truncated
        # body is the artifact that actually gets committed, so this is where
        # that class of bug would have done its damage.
        self.promote()
        text = self.skill.read_text()
        self.assertIn("## Search before filing", text)
        self.assertIn("## Link, do not duplicate", text)
        self.assertIn("Attach the thread to the existing issue.", text)

    def test_when_to_use_reaches_the_frontmatter(self):
        # The field discovery actually reads. Absent, the skill is on disk and
        # never fires, which looks identical to it not existing.
        self.promote("--when-to-use", "a customer reports a bug")
        front = self.skill.read_text().split("\n---\n")[0]
        self.assertIn("when_to_use:", front)
        self.assertIn("a customer reports a bug", front)

    def test_no_when_to_use_means_no_empty_field(self):
        self.promote()
        self.assertNotIn("when_to_use:", self.skill.read_text())

    def test_accepting_does_not_touch_the_entry_file(self):
        before, mtime = self.entry_file.read_bytes(), \
            self.entry_file.stat().st_mtime_ns
        self.promote()
        self.assertEqual(self.entry_file.read_bytes(), before)
        self.assertEqual(self.entry_file.stat().st_mtime_ns, mtime)

    def test_the_entry_becomes_promoted_without_losing_its_count(self):
        self.promote()
        entry = memory.find(memory.load(self.config), "filing-a-bug")
        self.assertEqual(entry.status, memory.PROMOTED)
        self.assertEqual(entry.count, 3)
        self.assertEqual(entry.body, self.BODY)

    def test_a_promoted_candidate_leaves_the_review_queue(self):
        self.promote()
        waiting = [e.name for e in memory.reviewable(memory.load(self.config))]
        self.assertNotIn("filing-a-bug", waiting)

    def test_promoting_twice_refuses_rather_than_overwriting(self):
        self.promote()
        self.skill.write_text("edited by hand\n")
        self.assertEqual(self.promote(), 1)
        self.assertEqual(self.skill.read_text(), "edited by hand\n")
        # And no second decision: a refused promotion that still logged one
        # would report a skill written at a path holding something else.
        self.assertEqual(len(self._decisions()), 1)

    def test_a_failed_write_logs_no_decision(self):
        # The ordering the command's docstring promises. A decision claiming a
        # skill exists when the file was never written is the worse failure:
        # nothing proposes the procedure again, so the loss is permanent.
        #
        # The OSError here is unhandled -- an unwritable skills directory exits
        # with a traceback rather than a message. Pinned rather than endorsed:
        # what this asserts is that the store stays clean either way.
        blocked = self.root / "blocked"
        blocked.write_text("not a directory\n")
        with self.assertRaises(OSError):
            from skillpp.cli import main
            main(["--root", str(self.config.root), "promote-candidate",
                  "filing-a-bug", "--skills-dir", str(blocked / "skills")])
        self.assertEqual(self._decisions(), [])

    def test_the_skills_dir_env_var_is_honoured(self):
        # Without this the eval harness cannot sandbox promotion, and a case
        # that wrongly accepts lands a SKILL.md in the developer's real
        # ~/.claude/skills.
        from skillpp.cli import main
        elsewhere = self.root / "by-env"
        with unittest.mock.patch.dict(
                os.environ, {"SKILLPP_SKILLS_DIR": str(elsewhere)}):
            self.assertEqual(main(["--root", str(self.config.root),
                                   "promote-candidate", "filing-a-bug"]), 0)
        self.assertTrue((elsewhere / "filing-a-bug" / "SKILL.md").is_file())

    def test_accepting_something_that_is_not_there_writes_nothing(self):
        from skillpp.cli import main
        self.assertEqual(main(["--root", str(self.config.root),
                               "promote-candidate", "no-such-candidate",
                               "--skills-dir", str(self.skills)]), 1)
        self.assertFalse(self.skills.exists())
        self.assertEqual(self._decisions(), [])


class TestTheMessageFloor(TempRoot):
    """The floor is a cost guard, and had to be overridable to be testable.

    It exists so a two-message increment does not spend a model call, and it
    loses nothing when it fires: the bookmark stays put and those messages
    arrive with the next batch. But it was a module constant, so a fixture
    built small on purpose was answered "stop" and the judgement under test was
    never put to the model at all -- six eval cases failed for that reason and
    none of them was about detection.
    """

    def test_the_default_is_unchanged(self):
        self.assertEqual(Config(self.root / "a").min_new_messages,
                         MIN_NEW_MESSAGES)

    def test_the_environment_can_lower_it(self):
        with unittest.mock.patch.dict(os.environ, {"SKILLPP_MIN_NEW": "1"}):
            self.assertEqual(Config(self.root / "b").min_new_messages, 1)

    def test_nonsense_falls_back_to_the_default(self):
        with unittest.mock.patch.dict(os.environ, {"SKILLPP_MIN_NEW": "many"}):
            self.assertEqual(Config(self.root / "c").min_new_messages,
                             MIN_NEW_MESSAGES)


class TestHeredocClipping(unittest.TestCase):
    """A heredoc body is content written to a file, not the command.

    `Write`'s content and `Edit`'s new_string are already capped at 250 here on
    a measured argument -- the step is "wrote the file", the content is the
    product. A heredoc is `Write` spelled in shell and had been getting Bash's
    full 1500, because flattening newlines before truncating left no delimiter
    to find. Measured at 4.8% of a 12 MB session's extract, and 12% of one that
    staged every commit message through /tmp.
    """

    def cmd(self, body: str, *, tail: str = "\ngit commit -F /tmp/m.txt") -> str:
        return f"cat > /tmp/m.txt <<'EOF'\n{body}\nEOF{tail}"

    def test_a_long_body_is_clipped_but_its_opening_survives(self):
        from skillpp.extract import _clip_heredocs
        body = "feat(x): the subject line\n" + "filler that repeats.\n" * 40
        out = _clip_heredocs(self.cmd(body))
        self.assertLess(len(out), len(self.cmd(body)))
        # A convention is stated at the top of a file, which is the half kept.
        self.assertIn("feat(x): the subject line", out)
        self.assertIn("chars written", out)

    def test_the_command_around_it_is_untouched(self):
        # The reason not to drop the body outright: what follows the closing
        # delimiter is often the step that matters.
        from skillpp.extract import _clip_heredocs
        out = _clip_heredocs(self.cmd("x\n" * 200))
        self.assertIn("cat > /tmp/m.txt <<'EOF'", out)
        self.assertIn("EOF", out)
        self.assertIn("git commit -F /tmp/m.txt", out)

    def test_a_short_body_is_left_alone(self):
        from skillpp.extract import _clip_heredocs
        original = self.cmd("feat: one line")
        self.assertEqual(_clip_heredocs(original), original)

    def test_an_unterminated_heredoc_is_left_alone(self):
        # No closing delimiter means no way to tell body from the rest of the
        # command, and guessing would eat a real step.
        from skillpp.extract import _clip_heredocs
        original = "cat > f <<'EOF'\n" + "line\n" * 200
        self.assertEqual(_clip_heredocs(original), original)

    def test_two_heredocs_in_one_command_are_both_clipped(self):
        from skillpp.extract import _clip_heredocs
        one = "a\n" * 200
        original = f"cat > x <<'A'\n{one}\nA\ncat > y <<'B'\n{one}\nB"
        out = _clip_heredocs(original)
        self.assertEqual(out.count("chars written"), 2)

    def test_it_runs_before_the_flattening_that_used_to_hide_it(self):
        # The regression guard. Flattening first leaves the body with no
        # delimiter, the clip silently matches nothing, and the only symptom is
        # a bigger prompt -- which no assertion would have caught.
        from skillpp.extract import _arguments
        body = "feat: subject\n" + "filler.\n" * 60
        rendered = _arguments("Bash", {"command": self.cmd(body)})
        self.assertIn("chars written", rendered)


class TestWindowing(unittest.TestCase):
    """Splitting a session into pieces a local model could read.

    Compression cannot close the gap -- twelve real sessions run 60k-128k tokens
    after a 47x reduction, and every further cut summed to about 5%. So the
    prompt is split instead, and the whole risk of splitting is that half a
    procedure reads as a finished one. That is how the capture branch failed,
    banking `ruff check` + `git commit` as a recipe of its own.
    """

    def session(self, *records: dict) -> list:
        from skillpp.transcript import read
        root = Path(tempfile.mkdtemp())
        return read(write_transcript(root, *records))

    def work(self, n: int, topic: str) -> list[dict]:
        """Tool calls with bulky arguments, so a budget can actually be hit."""
        out = []
        for i in range(n):
            call = {"type": "tool_use", "id": f"tu{topic}{i}", "name": "Bash",
                    "input": {"command": f"run --{topic}{i} " + "x" * 400}}
            out += [rec(f"a{topic}{i}", "assistant", [call]),
                    rec(f"r{topic}{i}", "user",
                        [tool_result("ok " + "y" * 400, tid=f"tu{topic}{i}")])]
        return out

    def test_a_boundary_only_falls_where_a_person_typed(self):
        from skillpp.window import segments
        msgs = self.session(rec("u1", content="first ask"), *self.work(2, "a"),
                            rec("u2", content="second ask"), *self.work(2, "b"))
        segs = segments(msgs)
        self.assertEqual(len(segs), 2)
        self.assertIn("first ask", segs[0].ask)
        self.assertIn("second ask", segs[1].ask)

    def test_a_tool_result_is_not_a_new_request(self):
        # Every call is followed by a user-role message carrying its result. If
        # those counted, a segment would break between a call and its own
        # output and every procedure would be shredded.
        from skillpp.window import segments
        msgs = self.session(rec("u1", content="one ask"), *self.work(6, "a"))
        self.assertEqual(len(segments(msgs)), 1)

    def test_an_injected_message_is_not_a_new_request(self):
        # Measured at 1.2% of real boundaries. A task notification arriving
        # mid-procedure would break it at exactly the wrong place.
        from skillpp.window import segments
        msgs = self.session(
            rec("u1", content="the ask"), *self.work(2, "a"),
            rec("u2", content="<task-notification>\nbackground job done"),
            *self.work(2, "b"))
        self.assertEqual(len(segments(msgs)), 1)

    def test_a_segment_is_never_split_across_windows(self):
        from skillpp.window import windows
        msgs = self.session(rec("u1", content="ask one"), *self.work(20, "a"),
                            rec("u2", content="ask two"), *self.work(20, "b"))
        wins = windows(msgs, budget=2_000, overlap=0)
        seen = [s.ask for w in wins for s in w.segments]
        # Two segments, each landing whole in exactly one window.
        self.assertEqual(len(seen), len(set(seen)))

    def test_an_oversized_segment_gets_its_own_window(self):
        # 3 of 1,758 real segments exceed an 8k budget alone, the largest
        # 11,940 tokens. Cutting one would separate a request from its work.
        from skillpp.window import windows
        msgs = self.session(rec("u1", content="small"), *self.work(1, "a"),
                            rec("u2", content="huge"), *self.work(40, "b"))
        wins = windows(msgs, budget=1_000, overlap=0)
        big = [w for w in wins if w.tokens > 1_000]
        self.assertEqual(len(big), 1)
        self.assertEqual(len(big[0].segments), 1)

    def test_overlap_keeps_a_straddling_procedure_whole_somewhere(self):
        # The property the overlap exists for. Without it a procedure lying
        # across a boundary is in no window whole, and a half is what gets
        # read as finished work. The carry is a token budget, so it is sized
        # here from a real segment rather than as a count.
        from skillpp.window import segments, windows
        msgs = self.session(rec("u1", content="ask A"), *self.work(4, "a"),
                            rec("u2", content="ask B"), *self.work(4, "b"),
                            rec("u3", content="ask C"), *self.work(4, "c"))
        segs = segments(msgs)
        wins = windows(msgs, budget=segs[0].tokens + 1,
                       overlap=segs[0].tokens)
        pairs = [("ask A", "ask B"), ("ask B", "ask C")]
        for first, second in pairs:
            intact = any(first in w.text and second in w.text for w in wins)
            self.assertTrue(intact, f"{first}+{second} split across every window")

    def test_no_overlap_means_no_such_guarantee(self):
        # Stated as a test so the overlap is never removed as dead weight.
        from skillpp.window import segments, windows
        msgs = self.session(rec("u1", content="ask A"), *self.work(4, "a"),
                            rec("u2", content="ask B"), *self.work(4, "b"))
        # Derived rather than hard-coded: a budget that fits one segment and
        # not two is the only one that puts a boundary between them, and the
        # extractor's compression ratio is not a number to guess at.
        segs = segments(msgs)
        budget = segs[0].tokens + 1
        wins = windows(msgs, budget=budget, overlap=0)
        self.assertGreater(len(wins), 1)
        self.assertFalse(any("ask A" in w.text and "ask B" in w.text
                             for w in wins))

    def test_the_carry_is_tokens_not_a_segment_count(self):
        """The regression guard on a 36-point difference in duplicated input.

        Carrying a *count* of segments duplicated 48% of eight real sessions,
        because the segments at the end of a full window are the large ones. A
        token budget does the same job for 12%. Nothing else in the module
        would notice if this reverted -- the windows would still be correct,
        just four times more expensive to read.
        """
        from skillpp.window import _carry, Segment
        segs = [Segment(messages=[], text="z" * 40_000),   # 10,000 tokens
                Segment(messages=[], text="x" * 400),      # 100
                Segment(messages=[], text="y" * 400)]      # 100
        # A count of two would carry 200 tokens here and 10,100 if the order
        # were reversed. A budget carries the same 200 either way, which is the
        # whole point: cost stops depending on where the boundary happens to
        # land.
        self.assertEqual([s.tokens for s in _carry(segs, 300)], [100, 100])
        self.assertEqual([s.tokens for s in _carry(segs, 1_000)], [100, 100])

    def test_the_carry_never_takes_half_a_segment(self):
        # Half a segment carried forward is half a request, which is the thing
        # this module exists to avoid.
        from skillpp.window import _carry, Segment
        segs = [Segment(messages=[], text="x" * 40_000)]
        self.assertEqual(_carry(segs, 1_000), [])

    def test_an_oversized_last_segment_carries_nothing_at_all(self):
        """A limit worth knowing rather than discovering.

        The carry has to be contiguous with the boundary: reaching past a
        segment that will not fit, to take smaller ones behind it, would leave
        the next window starting mid-session with a request missing from the
        middle. So it stops instead -- and the overlap guarantee quietly
        disappears at any boundary whose last segment is larger than the carry.
        Measured, that is the p90 segment, so roughly one boundary in ten.
        """
        from skillpp.window import _carry, Segment
        segs = [Segment(messages=[], text="x" * 400),
                Segment(messages=[], text="y" * 400),
                Segment(messages=[], text="z" * 40_000)]
        self.assertEqual(_carry(segs, 1_000), [])


class TestReconcile(unittest.TestCase):
    """Putting per-window findings back together.

    Two forces pulling opposite ways. The overlap shows one procedure to two
    windows, so the same finding arrives twice and must not count twice. A
    boundary splits one procedure across two windows, so two halves must become
    one. Dedupe too eagerly and a split collapses to a half; merge too eagerly
    and two procedures become one entry — which is the silent failure, because
    it inflates a count and discards a proposal with nothing recording it.
    """

    def find(self, name: str, span: tuple[int, int], *,
             complete: bool = True, window: int = 0):
        from skillpp.reconcile import Finding
        return Finding(name=name, body=f"body of {name}", window=window,
                       span=span, complete=complete)

    def test_the_same_procedure_in_overlapping_windows_is_one(self):
        from skillpp.reconcile import DUPLICATE, reconcile, settled
        groups = reconcile([self.find("filing-a-bug", (4, 8), window=0),
                            self.find("filing-a-bug", (7, 12), window=1)])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].verdict, DUPLICATE)
        self.assertEqual(len(settled(groups)), 1)

    def test_the_wider_view_of_a_duplicate_is_the_one_kept(self):
        # Both windows saw the procedure; the one that saw more of it wrote its
        # body from more of it.
        from skillpp.reconcile import reconcile
        groups = reconcile([self.find("filing-a-bug", (7, 8), window=1),
                            self.find("filing-a-bug", (4, 8), window=0)])
        self.assertEqual(groups[0].best.span, (4, 8))

    def test_a_partial_never_wins_over_a_whole_one(self):
        from skillpp.reconcile import reconcile
        groups = reconcile([
            self.find("filing-a-bug", (2, 9), window=0, complete=False),
            self.find("filing-a-bug", (8, 10), window=1, complete=True)])
        self.assertTrue(groups[0].best.complete)

    def test_the_same_name_far_apart_is_a_recurrence_not_a_duplicate(self):
        """The rule that is easy to get wrong by matching on names.

        A procedure done twice in one session is two sightings of one entry —
        what `their-recurs` tests. Folding them together because the names
        match destroys the evidence the entry earns its place.
        """
        from skillpp.reconcile import RECURRENCE, reconcile, settled
        groups = reconcile([self.find("rolling-out-a-release", (1, 3)),
                            self.find("rolling-out-a-release", (11, 14))])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].verdict, RECURRENCE)
        # Both survive: the count depends on them staying separate.
        self.assertEqual(len(settled(groups)), 2)

    def test_adjacent_halves_are_a_question_not_a_decision(self):
        # The case a boundary creates. Whether these are one procedure or two
        # is a judgement, and guessing it either way is the failure.
        from skillpp.reconcile import SPLIT, questions, reconcile
        groups = reconcile([self.find("searching-the-tracker", (5, 7)),
                            self.find("linking-the-thread", (8, 10))])
        self.assertEqual(groups[0].verdict, SPLIT)
        self.assertEqual(len(questions(groups)), 1)

    def test_two_names_over_the_same_segments_is_a_question(self):
        from skillpp.reconcile import AMBIGUOUS, questions, reconcile
        groups = reconcile([self.find("filing-a-bug", (4, 9)),
                            self.find("triaging-support", (5, 8))])
        self.assertEqual(groups[0].verdict, AMBIGUOUS)
        self.assertEqual(len(questions(groups)), 1)

    def test_unrelated_work_asks_nothing(self):
        from skillpp.reconcile import DISTINCT, questions, reconcile, settled
        groups = reconcile([self.find("filing-a-bug", (0, 2)),
                            self.find("cutting-a-tag", (9, 11))])
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(g.verdict == DISTINCT for g in groups))
        self.assertEqual(questions(groups), [])
        self.assertEqual(len(settled(groups)), 2)

    def test_a_procedure_across_three_windows_becomes_one_group(self):
        # Built transitively: A touches B and B touches C, so all three are one
        # question rather than two unrelated ones.
        from skillpp.reconcile import reconcile
        groups = reconcile([self.find("part-one", (0, 3), window=0),
                            self.find("part-two", (4, 6), window=1),
                            self.find("part-three", (7, 9), window=2)])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].findings), 3)
        self.assertEqual(groups[0].span, (0, 9))

    def test_one_ambiguous_pair_makes_the_whole_group_a_question(self):
        # A group settled on most of its pairs is still unsettled: resolving the
        # ambiguity is what decides whether the rest belongs together.
        from skillpp.reconcile import AMBIGUOUS, reconcile
        groups = reconcile([self.find("filing-a-bug", (2, 6), window=0),
                            self.find("filing-a-bug", (5, 9), window=1),
                            self.find("triaging-support", (5, 7), window=1)])
        self.assertEqual(groups[0].verdict, AMBIGUOUS)

    def test_names_differing_only_in_separators_are_the_same(self):
        from skillpp.reconcile import DUPLICATE, reconcile
        groups = reconcile([self.find("filing_a_bug", (1, 4)),
                            self.find("Filing-A-Bug", (3, 6))])
        self.assertEqual(groups[0].verdict, DUPLICATE)

    def test_nothing_in_nothing_out(self):
        from skillpp.reconcile import questions, reconcile, settled
        self.assertEqual(reconcile([]), [])
        self.assertEqual(settled([]), [])
        self.assertEqual(questions([]), [])


class TestSegmentsCommand(TempRoot):
    """The locator's input: one request and its work, numbered.

    Deterministic, so it is tested here rather than paid for in an eval. What
    matters is that the numbering the model answers against is the same
    numbering `reconcile` groups by — a mismatch there would score correct
    answers as wrong and be invisible in the output.
    """

    def transcript(self) -> Path:
        return write_transcript(
            self.root,
            rec("u1", content="first ask"),
            rec("a1", "assistant", [tool_use("Bash", command="npm test")]),
            rec("r1", "user", [tool_result("14 passing", tid="tu_Bash")]),
            rec("u2", content="second ask"),
            rec("a2", "assistant", [tool_use("Bash", command="git commit -am x")]),
            rec("r2", "user", [tool_result("[main abc] x", tid="tu_Bash")]))

    def run_it(self, target: Path) -> str:
        import io
        import contextlib
        from skillpp.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["--root", str(self.config.root), "segments", str(target)])
        self.assertEqual(code, 0)
        return buf.getvalue()

    def test_each_request_is_its_own_numbered_segment(self):
        out = self.run_it(self.transcript())
        self.assertIn("--- segment 0 ---", out)
        self.assertIn("--- segment 1 ---", out)
        self.assertNotIn("--- segment 2 ---", out)
        self.assertLess(out.index("first ask"), out.index("--- segment 1 ---"))

    def test_the_numbering_matches_what_reconcile_groups_by(self):
        # A silent-failure guard. If these ever diverge, correct answers score
        # as wrong and nothing in the output says so.
        from skillpp.transcript import read
        from skillpp.window import segments
        segs = segments(read(self.transcript()))
        out = self.run_it(self.transcript())
        for seg in segs:
            self.assertIn(f"--- segment {seg.index} ---", out)

    def test_an_unreadable_target_says_stop_rather_than_failing(self):
        # This output is substituted into a prompt, where an exit code cannot
        # be seen — so "there is nothing here" has to be a sentence.
        out = self.run_it(self.root / "does-not-exist.jsonl")
        self.assertIn("Stop here and write nothing", out)

    def test_an_empty_transcript_says_stop_too(self):
        empty = self.root / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        self.assertIn("Stop here", self.run_it(empty))


class TestAspectFilter(unittest.TestCase):
    """Showing a first pass one aspect of a request at a time.

    Measured on twelve real sessions: tools are 40.4% of a segment, narration
    31.7%, results 20.5%, prompts 7.4%. Showing only the tools cuts window
    boundaries from 155 to 58 at the same budget — and boundaries are what lose
    a procedure, so this is a recall measure as much as a cost one.

    Whether it *works* is a question for a model and is asked in the evals. What
    is deterministic, and tested here, is that the filter shows what it claims
    and nothing else.
    """

    SEG = ("> add a rate limit\n"
           "  $ Edit src/api/export.py\n"
           "    = edited\n"
           "  $ Bash npm test\n"
           "    ! 3 failing\n"
           "  . Fixed the ordering; suite green now.")

    def keep(self, *aspects: str) -> str:
        from skillpp.window import keep_aspects
        return keep_aspects(self.SEG, aspects)

    def test_each_aspect_shows_only_itself(self):
        self.assertEqual(self.keep("prompts").strip(), "> add a rate limit")
        self.assertIn("$ Edit", self.keep("tools"))
        self.assertNotIn("= edited", self.keep("tools"))
        self.assertNotIn("Fixed the ordering", self.keep("tools"))

    def test_a_continuation_line_follows_the_marker_that_opened_it(self):
        # Filtering has to be stateful: a wrapped result has no marker of its
        # own, and leaking it into a tools-only view would defeat the point.
        from skillpp.window import keep_aspects
        wrapped = "  $ Bash x\n    = line one\n      line two wrapped\n  . said"
        self.assertNotIn("line two wrapped", keep_aspects(wrapped, ("tools",)))
        self.assertIn("line two wrapped", keep_aspects(wrapped, ("results",)))

    def test_failures_are_their_own_aspect(self):
        # 0.8% of a corpus and the reason a route took its shape, so something
        # showing only tools may still want them.
        self.assertNotIn("3 failing", self.keep("tools", "results"))
        self.assertIn("3 failing", self.keep("tools", "failures"))

    def test_asking_for_everything_changes_nothing(self):
        from skillpp.window import DEFAULT_ASPECTS, keep_aspects
        kept = keep_aspects(self.SEG, DEFAULT_ASPECTS)
        for line in self.SEG.splitlines():
            self.assertIn(line.strip(), kept)

    def test_an_empty_result_is_still_a_segment(self):
        # The numbering the model answers against and the one `reconcile`
        # groups by must not drift, so a segment with nothing of the shown
        # aspect stays in place rather than being dropped.
        self.assertEqual(self.keep("failures").strip(), "! 3 failing")
        self.assertEqual(self.keep("narration").strip(),
                         ". Fixed the ordering; suite green now.")


class TestTheTwoLocatePrompts(unittest.TestCase):
    """`/locate` and `/locate-one` must define the verdicts identically.

    They ask the same question at different granularity — every segment in one
    call, or one segment per call — and the definition of `landed` is duplicated
    because a command file cannot include another. Duplicated text drifts, and
    the drift here would be invisible: both prompts keep working, their answers
    stop being comparable, and the whole point of running one against the other
    is that they are.

    `landed` in this block has already been rewritten twice. Both times the
    frontier arm had to be re-verified against 18 cases, which is exactly the
    cost that makes silent divergence expensive.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def block(self, name: str) -> str:
        text = (self.ROOT / ".claude" / "commands" / name).read_text()
        start = text.index("- `landed` —")
        end = text.index("without one they mean nothing.")
        return text[start:end]

    def test_the_verdict_definition_is_the_same_text(self):
        self.assertEqual(self.block("locate.md"), self.block("locate-one.md"))

    def test_both_still_define_landed_as_two_halves(self):
        # A guard on the substance, not just on equality: editing both files
        # identically in the wrong direction would pass the test above.
        for name in ("locate.md", "locate-one.md"):
            with self.subTest(name):
                self.assertIn("carried out, and then confirmed",
                              self.block(name))
                self.assertIn("Both halves are", self.block(name))

    def test_the_distributable_copies_match_the_active_ones(self):
        # install.py copies from commands/, so a fix applied only to
        # .claude/commands/ ships the old prompt.
        for name in ("locate.md", "locate-one.md", "log-session.md",
                     "review-candidates.md"):
            with self.subTest(name):
                self.assertEqual(
                    (self.ROOT / ".claude" / "commands" / name).read_text(),
                    (self.ROOT / "commands" / name).read_text())

    def test_only_the_all_at_once_prompt_shows_a_numbered_example(self):
        """The single-segment prompt must not give a copyable answer shape.

        A 7B model returned `0 landed / 1 open / 2 open` — the example from
        `locate.md` — for three different fixtures, including one with a single
        segment. Asking about one segment removes the numbering, and the answer
        format has to stay unnumbered or the same failure returns.
        """
        one = (self.ROOT / ".claude" / "commands" / "locate-one.md").read_text()
        self.assertNotRegex(one, r"^\s*\d+\s+(landed|open)\s*$")
        self.assertIn("One word, and nothing else", one)


class TestLocalSpans(unittest.TestCase):
    """Turning per-request verdicts into stretches a judge should read.

    No model involved: this is the part that combines answers, and it is the
    part that decides what an expensive read costs. Tested here because it is
    deterministic, unlike anything that produced the verdicts.
    """

    def verdicts(self, *landed: bool | None):
        from skillpp.local import Verdict
        return [Verdict(index=i, landed=v, why="") for i, v in enumerate(landed)]

    def test_a_span_runs_from_after_the_last_landing_to_this_one(self):
        from skillpp.local import spans
        # open, open, landed  ->  one span covering all three: the procedure
        # began when the work began, not at the request that finished it.
        self.assertEqual(spans(self.verdicts(False, False, True)), [(0, 2)])

    def test_two_landings_are_two_spans(self):
        from skillpp.local import spans
        self.assertEqual(spans(self.verdicts(True, False, True)),
                         [(0, 0), (1, 2)])

    def test_trailing_unresolved_work_is_not_a_span(self):
        # Work still open when the session ended is not a procedure to read.
        from skillpp.local import spans
        self.assertEqual(spans(self.verdicts(True, False, False)), [(0, 0)])

    def test_nothing_landing_means_nothing_to_read(self):
        from skillpp.local import spans
        self.assertEqual(spans(self.verdicts(False, False, False)), [])

    def test_an_escalated_request_never_ends_a_span(self):
        """Uncertainty shows a judge more, never less.

        A request the models disagreed about is exactly the one a judge should
        see in context. Treating it as a landing would cut the span there and
        hand over the half before it.
        """
        from skillpp.local import spans
        self.assertEqual(spans(self.verdicts(False, None, True)), [(0, 2)])

    def test_disagreement_escalates_rather_than_picking_a_side(self):
        from skillpp.local import Verdict
        v = Verdict(index=0, landed=None, why="qwen: no vs granite: yes")
        self.assertTrue(v.escalates)
        self.assertFalse(Verdict(index=0, landed=True, why="").escalates)

    def test_a_missing_model_raises_rather_than_reporting_an_empty_session(self):
        # A silent fallback would report "nothing landed" for a session the
        # models never saw, which is indistinguishable from a quiet session.
        from skillpp.local import LocalModelUnavailable, verdicts
        from skillpp.window import Segment
        with unittest.mock.patch("skillpp.local._ask",
                                 side_effect=LocalModelUnavailable("no daemon")):
            with self.assertRaises(LocalModelUnavailable):
                verdicts([Segment(messages=[], text="> x", index=0)])

    def test_the_context_is_sized_for_the_prompt(self):
        """The bug a real session found and 21 toy fixtures could not.

        Ollama defaults num_ctx to 4096 and truncates a longer prompt from the
        front, where the instructions are. Five of forty real segments exceeded
        that; none of the label-set segments comes close, because they are 102
        tokens at the median against 869 for real ones.
        """
        from skillpp.local import CTX_CEILING, CTX_FLOOR, _context_for
        self.assertEqual(_context_for("x" * 400), CTX_FLOOR)
        self.assertEqual(_context_for("x" * 4 * 4_000), 8_192)
        self.assertEqual(_context_for("x" * 4 * 9_000), 16_384)
        # Never past the ceiling: allocating for the worst case would not fit.
        self.assertEqual(_context_for("x" * 4 * 40_000), CTX_CEILING)


class TestPrepareSessionWindows(TempRoot):
    """Windowing wired into the pipeline, opt-in.

    Whole-session rendering is what the verified eval arm measures. Real
    sessions render at 60k-128k tokens against a largest tested case of 16k, so
    the flags exist to A/B that gap rather than close it by assumption — which
    means the default path must stay byte-identical.
    """

    def session(self) -> Path:
        records = []
        for n in range(8):
            records += [
                rec(f"u{n}", content=f"request number {n} " + "context " * 40),
                rec(f"a{n}", "assistant",
                    [tool_use("Bash", command=f"run --step{n} " + "x" * 300)]),
                rec(f"r{n}", "user",
                    [tool_result("ok " + "y" * 300, tid="tu_Bash")]),
            ]
        return write_transcript(self.root, *records, name="w.jsonl")

    def run_it(self, *extra: str) -> str:
        import contextlib
        import io
        from skillpp.cli import main
        buf = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"SKILLPP_MIN_NEW": "1"}):
            with contextlib.redirect_stdout(buf):
                self.assertEqual(
                    main(["--root", str(self.config.root), "prepare-session",
                          str(self.session()), *extra]), 0)
        return buf.getvalue()

    def test_the_default_path_is_untouched(self):
        plain = self.run_it()
        self.assertIn("# New in this session", plain)
        self.assertNotIn("window ", plain.split("\n")[0])

    def test_listing_windows_prints_no_session_content(self):
        listed = self.run_it("--windows")
        self.assertIn("window(s) over", listed)
        self.assertNotIn("request number 0 ", listed)

    def test_one_window_names_its_own_requests(self):
        one = self.run_it("--window", "0")
        self.assertIn("window         0 of", one)
        self.assertRegex(one, r"requests\s+\d+-\d+")

    def test_the_windows_together_cover_every_request(self):
        # A window that quietly dropped a request would lose a procedure with
        # nothing reporting it.
        listed = self.run_it("--windows")
        spans = re.findall(r"requests (\d+)-(\d+)", listed)
        covered = {i for a, b in spans for i in range(int(a), int(b) + 1)}
        self.assertEqual(covered, set(range(max(covered) + 1)))

    def test_asking_for_a_window_that_does_not_exist_says_stop(self):
        # This output goes into a prompt, where an exit code cannot be seen.
        self.assertIn("Stop here and write nothing",
                      self.run_it("--window", "99"))


class TestTheWholeLoop(TempRoot):
    """Record, record, record, cross the threshold, accept, get a skill.

    The three things this product has to do are: notice a procedure, recognise
    it again and count it, and turn an accepted one into a skill on disk. The
    first two have eval cases (`new`, `match`). The third had none — the review
    eval stops at the question a person answers, and the accept-path tests call
    `promote-candidate` on a store that was seeded rather than accumulated. Both
    ends were covered and the join between them was not.

    Everything here goes through the real CLI, with nothing seeded: the count
    reaches three because three sessions recorded it, and the promotion names
    the candidate the queue actually offered. The one thing left out is pressing
    the button, which `claude -p` cannot do.
    """

    BODY = ("Read the intake spec first — the required fields change.\n\n"
            "## Search by signature, not by title\n\n"
            "Titles get worded differently every time.\n\n"
            "## Link, do not duplicate\n\n"
            "Attach the thread to the existing issue.")

    def cli(self, *args: str, stdin: str | None = None) -> str:
        """Run the real command, capturing what it prints."""
        import contextlib
        import io
        from skillpp.cli import main
        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(buf))
            if stdin is not None:
                stack.enter_context(unittest.mock.patch("sys.stdin",
                                                        io.StringIO(stdin)))
            code = main(["--root", str(self.config.root), *args])
        self.assertEqual(code, 0, buf.getvalue())
        return buf.getvalue()

    def transcript(self, tag: str) -> Path:
        """A distinct session that did the procedure.

        Distinct matters: the count is distinct sessions, so three recordings
        against one transcript would stay at one however many times they ran.
        """
        return write_transcript(
            self.root,
            rec(f"{tag}u1", content=f"a customer reported something ({tag})"),
            rec(f"{tag}a1", "assistant",
                [tool_use("Bash", command=f"gh issue list --search {tag}")]),
            rec(f"{tag}r1", "user", [tool_result("no match", tid="tu_Bash")]),
            rec(f"{tag}a2", "assistant",
                [tool_use("Bash", command=f"gh issue create --title {tag}")]),
            rec(f"{tag}r2", "user", [tool_result("created #1", tid="tu_Bash")]),
            name=f"{tag}.jsonl")

    def record(self, tag: str, *, name: str, matches: str | None = None):
        args = ["record-candidate", str(self.transcript(tag)), "--name", name]
        if matches:
            args += ["--matches", matches]
        return self.cli(*args, stdin=self.BODY)

    def test_three_sessions_reach_the_threshold_and_promote_to_a_skill(self):
        from skillpp import memory
        NAME = "filing-a-bug-from-a-support-report"

        # 1. First sighting. A procedure nobody has seen before.
        self.record("s1", name=NAME)
        entry = memory.find(memory.load(self.config), NAME)
        self.assertEqual(entry.count, 1)
        self.assertEqual(memory.reviewable(memory.load(self.config)), [],
                         "one sighting must not reach the review queue")

        # 2. Seen again, in a different session, matched against the first.
        self.record("s2", name="filing-a-bug", matches=NAME)
        self.assertEqual(memory.find(memory.load(self.config), NAME).count, 2)
        self.assertEqual(memory.reviewable(memory.load(self.config)), [],
                         "two sightings is still below the threshold")

        # 3. Third distinct session crosses it.
        self.record("s3", name="filing-a-bug", matches=NAME)
        store = memory.load(self.config)
        entry = memory.find(store, NAME)
        self.assertEqual(entry.count, 3)
        self.assertEqual([e.name for e in memory.reviewable(store)],
                         [NAME],
                         "three sightings must reach the review queue")

        # The body is still the one the first sighting taught, untouched by two
        # later recordings.
        self.assertEqual(entry.body, self.BODY)

        # 4. What the queue offers is what a person is asked about.
        listed = self.cli("candidates")
        self.assertIn(NAME, listed)
        self.assertIn("x3", listed)

        # 5. Accepted. This is the step that had no test joining it to the rest.
        skills = self.root / "skills"
        self.cli("promote-candidate", NAME,
                 "--skills-dir", str(skills),
                 "--description", "File a bug from a support report.",
                 "--when-to-use", "a customer reports a bug")

        skill = skills / NAME / "SKILL.md"
        self.assertTrue(skill.is_file(), "accepting produced no skill file")
        text = skill.read_text()
        # Everything discovery reads before loading the body.
        self.assertIn(f"name: {NAME}", text)
        self.assertIn("File a bug from a support report.", text)
        self.assertIn("a customer reports a bug", text)
        # And the procedure itself, whole.
        self.assertIn("## Search by signature, not by title", text)
        self.assertIn("## Link, do not duplicate", text)
        self.assertIn("seen: 3", text)

        # 6. It leaves the queue and does not come back.
        store = memory.load(self.config)
        self.assertEqual(memory.reviewable(store), [])
        promoted = memory.find(store, NAME)
        self.assertEqual(promoted.status, memory.PROMOTED)
        self.assertEqual(promoted.count, 3, "promotion must not reset the count")

        # 7. Doing the work again keeps it promoted and raises the count,
        #    rather than filing a duplicate candidate.
        self.record("s4", name="filing-a-bug", matches=NAME)
        again = memory.find(memory.load(self.config), NAME)
        self.assertEqual(again.count, 4)
        self.assertEqual(again.status, memory.PROMOTED)
        self.assertEqual(len(list(self.config.patterns_dir.glob("*.md"))), 1,
                         "a recurrence after promotion filed a second entry")


class TestTheInstalledHookCommand(unittest.TestCase):
    """The command in `.claude/settings.json`, run as a shell would run it.

    Every other queue test calls `enqueue` or `cmd_hook` directly, which is the
    Python API rather than the thing Claude Code actually executes. That gap
    hid a real failure: the command was `python3 bin/skillpp …`, a *relative*
    path, so it worked only when the hook happened to run from the repository
    root. Anywhere else the shell exited 2 before Python started -- so
    `cmd_hook`'s "always exit 0, never disrupt a session" never applied, nothing
    was queued, and `drain` reported "Nothing has ended yet", which is
    indistinguishable from a machine where no session has ended.
    """

    REPO = Path(__file__).resolve().parents[1]

    def command(self) -> str:
        settings = json.loads((self.REPO / ".claude" / "settings.json")
                              .read_text(encoding="utf-8"))
        entries = settings["hooks"]["SessionEnd"]
        return entries[0]["hooks"][0]["command"]

    def fire(self, cwd, env_extra):
        """Run the tracked command from *cwd*, and report what it queued."""
        import subprocess
        with tempfile.TemporaryDirectory() as project:
            transcript = Path(project) / "t.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            payload = json.dumps({"session_id": "installed-cmd",
                                  "transcript_path": str(transcript),
                                  "cwd": project,
                                  "hook_event_name": "SessionEnd"})
            done = subprocess.run(
                ["sh", "-c", self.command()], input=payload, text=True,
                capture_output=True, cwd=str(cwd),
                env={**os.environ, **env_extra})
            queue = (Path(project) / ".claude" / "skillpp" / "memory"
                     / "ended-sessions.jsonl")
            rows = (queue.read_text(encoding="utf-8").splitlines()
                    if queue.is_file() else [])
            return done.returncode, rows

    def test_it_queues_when_run_from_the_project_root(self):
        code, rows = self.fire(self.REPO, {"CLAUDE_PROJECT_DIR": str(self.REPO)})
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)

    def test_it_queues_when_run_from_somewhere_else_entirely(self):
        """The case the relative path got wrong.

        A hook is not promised the project root as its working directory, and a
        wrong guess here fails silently rather than loudly.
        """
        with tempfile.TemporaryDirectory() as elsewhere:
            code, rows = self.fire(elsewhere,
                                   {"CLAUDE_PROJECT_DIR": str(self.REPO)})
        self.assertEqual(code, 0, "the shell could not find the entry point")
        self.assertEqual(len(rows), 1, "nothing was queued, and nothing said so")

    def test_it_still_works_where_the_project_dir_is_not_set(self):
        """The fallback keeps the previous behaviour rather than replacing it."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        import subprocess
        with tempfile.TemporaryDirectory() as project:
            transcript = Path(project) / "t.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            payload = json.dumps({"session_id": "no-var",
                                  "transcript_path": str(transcript),
                                  "cwd": project,
                                  "hook_event_name": "SessionEnd"})
            done = subprocess.run(["sh", "-c", self.command()], input=payload,
                                  text=True, capture_output=True,
                                  cwd=str(self.REPO), env=env)
            queue = (Path(project) / ".claude" / "skillpp" / "memory"
                     / "ended-sessions.jsonl")
            self.assertEqual(done.returncode, 0)
            self.assertTrue(queue.is_file())


class TestTheQueueTheHookWrites(unittest.TestCase):
    """What `SessionEnd` banks, and that `drain` can read it back.

    These two halves used to name the queue path independently — the reader in
    `cmd_drain`, the writer in a hand-typed one-liner in `.claude/settings.json`.
    A writer and reader that disagree produce an empty queue that looks exactly
    like "no sessions ended yet", so the shared path is the thing under test.
    """

    def payload(self, project: Path, session: str = "s-1",
                transcript: str | None = None) -> dict:
        return {"hook_event_name": "SessionEnd", "session_id": session,
                "transcript_path": transcript if transcript is not None
                else str(project / "t.jsonl"),
                "cwd": str(project)}

    def test_the_hook_writes_where_drain_looks(self):
        from skillpp.queue import enqueue, queue_path
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "t.jsonl").write_text('{"type":"user"}\n')
            result = enqueue(self.payload(project))
            self.assertEqual(result["status"], "queued")
            self.assertEqual(Path(result["queue"]), queue_path(project))
            rows = [json.loads(l) for l
                    in queue_path(project).read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        # The two fields cmd_drain reads off every row.
        self.assertEqual(rows[0]["session_id"], "s-1")
        self.assertTrue(rows[0]["transcript_path"].endswith("t.jsonl"))

    def test_a_session_with_no_transcript_is_not_banked(self):
        from skillpp.queue import enqueue, queue_path
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            gone = self.payload(project, transcript=str(project / "absent.jsonl"))
            self.assertEqual(enqueue(gone)["status"], "no-transcript")
            self.assertEqual(enqueue(self.payload(project, transcript=""))[
                "status"], "no-transcript")
            self.assertFalse(queue_path(project).exists(),
                             "an unusable row created the queue file anyway")

    def test_the_queue_appends_rather_than_overwrites(self):
        from skillpp.queue import enqueue, queue_path
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "t.jsonl").write_text('{"type":"user"}\n')
            for session in ("s-1", "s-2", "s-2"):
                enqueue(self.payload(project, session=session))
            rows = queue_path(project).read_text().strip().splitlines()
        self.assertEqual(len(rows), 3, "a repeated session is banked, not merged")

    def test_the_queue_is_project_local_not_store_local(self):
        """Two projects draining separately must not read each other's queue."""
        from skillpp.queue import enqueue, queue_path
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            for p in (a, b):
                p.mkdir()
                (p / "t.jsonl").write_text('{"type":"user"}\n')
            enqueue(self.payload(a, session="in-a"))
            self.assertNotEqual(queue_path(a), queue_path(b))
            self.assertFalse(queue_path(b).exists())
            self.assertIn("in-a", queue_path(a).read_text())

    def test_the_hook_only_queues_on_session_end(self):
        """PostToolUse used to be captured. Nothing reads that, so it is ignored."""
        from skillpp.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "t.jsonl").write_text('{"type":"user"}\n')
            from skillpp.queue import queue_path
            for event in ("PostToolUse", "UserPromptSubmit"):
                payload = dict(self.payload(project), hook_event_name=event)
                stdin, sys.stdin = sys.stdin, io.StringIO(json.dumps(payload))
                try:
                    self.assertEqual(main(["--root", tmp, "hook"]), 0)
                finally:
                    sys.stdin = stdin
            self.assertFalse(queue_path(project).exists(),
                             "a non-SessionEnd event reached the queue")

    def test_a_hook_failure_never_breaks_the_session(self):
        """Exit 0 whatever happens — a hook that fails must not disrupt work."""
        from skillpp.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            stdin, sys.stdin = sys.stdin, io.StringIO("not json at all")
            try:
                self.assertEqual(main(["--root", tmp, "hook"]), 0)
            finally:
                sys.stdin = stdin


class TestDrain(TempRoot):
    """Emptying the queue of sessions that ended while nobody looked.

    The SessionEnd hook has been appending for weeks and nothing ever read the
    file: 159 queued, 157 unreviewed, which is the whole distance between
    "watches how work gets done" and "waits to be asked".
    """

    def queue(self, *rows: dict) -> Path:
        path = self.root / "ended-sessions.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows),
                        encoding="utf-8")
        return path

    def transcript(self, tag: str) -> Path:
        """A session with enough in it to be worth a call.

        Above MIN_NEW_MESSAGES on purpose: the drain now skips thinner ones in
        code, so a one-message transcript would test the floor rather than the
        queue handling these tests are about.
        """
        records = []
        for n in range(MIN_NEW_MESSAGES + 2):
            records.append(rec(f"{tag}u{n}", content=f"request {n}"))
        return write_transcript(self.root, *records, name=f"{tag}.jsonl")

    def drain(self, queue: Path, *extra: str) -> str:
        import contextlib
        import io
        from skillpp.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["--root", str(self.config.root), "drain",
                                   "--queue", str(queue), *extra]), 0)
        return buf.getvalue()

    def test_it_is_a_dry_run_unless_told_otherwise(self):
        # Each application is a model call, so spending them cannot be the
        # default.
        out = self.drain(self.queue(
            {"session_id": "a", "transcript_path": str(self.transcript("a"))}))
        self.assertIn("Dry run", out)
        self.assertIn("--no-session-persistence", out)

    def test_a_missing_transcript_is_counted_not_an_error(self):
        # 143 of 157 real queue entries pointed at a file that no longer
        # existed. Treating that as a failure would stop the drain on its first
        # entry, every time.
        out = self.drain(self.queue(
            {"session_id": "gone", "transcript_path": "/nope/missing.jsonl"},
            {"session_id": "here", "transcript_path": str(self.transcript("h"))}))
        self.assertIn("transcript gone 1", out)
        self.assertIn("to review      1", out)

    def test_an_already_reviewed_session_is_skipped(self):
        self.config.reviews_dir.mkdir(parents=True, exist_ok=True)
        (self.config.reviews_dir / "old.md").write_text("reviewed", "utf-8")
        out = self.drain(self.queue(
            {"session_id": "old", "transcript_path": str(self.transcript("o"))}))
        self.assertIn("reviewed       1", out)
        self.assertIn("to review      0", out)

    def test_a_session_queued_twice_is_reviewed_once(self):
        path = str(self.transcript("d"))
        out = self.drain(self.queue({"session_id": "d", "transcript_path": path},
                                    {"session_id": "d", "transcript_path": path}))
        self.assertIn("ended          1 sessions", out)

    def test_the_drain_never_spawns_a_persisted_session(self):
        """The trap that makes the queue refill itself.

        A `claude -p` run without --no-session-persistence is a session that
        ends, which the hook enqueues, which the next drain picks up. The flag
        is asserted in the dry-run output because that is the only place it can
        be checked without spending a call.
        """
        out = self.drain(self.queue(
            {"session_id": "x", "transcript_path": str(self.transcript("x"))}))
        for line in out.splitlines():
            if line.strip().startswith("claude -p"):
                self.assertIn("--no-session-persistence", line)

    def test_no_queue_is_not_a_failure(self):
        out = self.drain(self.root / "absent.jsonl")
        self.assertIn("No queue at", out)

    def test_a_corrupt_line_does_not_stop_the_drain(self):
        path = self.root / "q.jsonl"
        path.write_text('{"session_id": "a", "transcript_path": "'
                        + str(self.transcript("a")) + '"}\nnot json\n\n',
                        encoding="utf-8")
        self.assertIn("to review      1", self.drain(path))

    def test_pruning_drops_only_the_unreviewable(self):
        """A `claude -p` run leaves a dead queue entry every time.

        Such a run ends like any session and fires the SessionEnd hook, but
        --no-session-persistence means it writes no transcript — so the entry
        points at a file that never existed. Measured on the real queue: 158 of
        174 entries were that, which is every eval sweep and every drain call
        ever made. The hook now skips them at the source; this clears a backlog.
        """
        live = str(self.transcript("keep"))
        queue = self.queue(
            {"session_id": "dead1", "transcript_path": "/gone/a.jsonl"},
            {"session_id": "keep", "transcript_path": live},
            {"session_id": "dead2", "transcript_path": "/gone/b.jsonl"})
        out = self.drain(queue, "--prune")
        self.assertIn("pruned         2 entries", out)
        left = [json.loads(l) for l in queue.read_text().splitlines() if l.strip()]
        self.assertEqual([r["session_id"] for r in left], ["keep"])

    def test_a_thin_session_is_skipped_without_a_model_call(self):
        """The floor is a code decision and was being paid for with a call.

        A session below the floor writes no review file, so it is never marked
        reviewed and returns on every drain. Measured: five sessions spent five
        model calls to be told "8 messages, below 25", and would have spent five
        more on the next drain, forever.
        """
        thin = write_transcript(self.root, rec("t1", content="tiny"),
                                name="thin.jsonl")
        out = self.drain(self.queue(
            {"session_id": "thin", "transcript_path": str(thin)}))
        self.assertIn("below the floor 1", out)
        self.assertIn("to review      0", out)
        # And it costs nothing: no command is printed for it.
        self.assertNotIn("claude -p", out)

    def test_pruning_says_nothing_when_there_is_nothing_dead(self):
        queue = self.queue({"session_id": "a",
                            "transcript_path": str(self.transcript("a"))})
        self.assertNotIn("pruned", self.drain(queue, "--prune"))

    def test_triage_is_off_unless_asked_for(self):
        # It is a saving with a risk attached: a session it skips is never
        # revisited. Opt-in, so a drain never silently loses work.
        out = self.drain(self.queue(
            {"session_id": "a", "transcript_path": str(self.transcript("a"))}))
        self.assertNotIn("triaged out", out)
        self.assertIn("to review      1", out)

    def test_an_unreachable_local_model_sends_the_session_on(self):
        """A saving that loses work is not a saving.

        Ollama not running, not installed, or the model absent must degrade to
        today's behaviour — every session read by the judge — rather than to
        every session skipped.
        """
        from skillpp.local import LocalModelUnavailable, triage
        with unittest.mock.patch("skillpp.local._ask",
                                 side_effect=LocalModelUnavailable("down")):
            worth, why = triage("> do the thing\n  $ Bash make release\n"
                                "    = ok\n" * 4)
        self.assertTrue(worth)
        self.assertIn("unavailable", why)

    def test_an_unclear_answer_sends_the_session_on(self):
        from skillpp.local import triage
        with unittest.mock.patch("skillpp.local._ask",
                                 return_value="well, yes and no"):
            worth, why = triage("> do the thing\n  $ Bash make release\n"
                                "    = ok\n" * 4)
        self.assertTrue(worth)
        self.assertIn("no clear answer", why)

    def test_a_session_with_no_request_text_is_decided_without_a_call(self):
        from skillpp.local import triage
        with unittest.mock.patch("skillpp.local._ask") as asked:
            worth, why = triage("")
        asked.assert_not_called()
        self.assertFalse(worth)
        self.assertIn("no developer request", why)


class TestLocalMatching(TempRoot):
    """Deciding a match by embedding rather than by asking a model.

    Measured on the proposals the pipeline recorded across real sessions: two
    bodies the judge called the same procedure score 0.873 to 0.990, two it
    filed separately score 0.569 to 0.776. THRESHOLD sits in that gap.

    The tests here stub the embedding. What a real model returns is measured in
    `tests/evals/matching.py`; what must hold regardless is the plumbing —
    which way it fails, and that it never merges on a near miss.
    """

    BODY = "Read the intake spec first.\n\n## Search by signature\n\nNot title."

    def stub(self, scores: dict[str, float]):
        """Embeddings whose cosine reproduces the scores asked for.

        One dimension per name plus a slack dimension that makes the target a
        unit vector; each candidate is a basis vector, so cosine comes out as
        exactly the number given. The slack dimension is not decoration — with
        one candidate and no slack the vectors are one-dimensional and positive,
        so cosine is 1.0 whatever score was asked for, and a near-miss test
        passes when it should fail.
        """
        names = list(scores)
        slack = max(0.0, 1.0 - sum(scores[n] ** 2 for n in names)) ** 0.5

        def fake(text: str, **_kw):
            if text.startswith(self.BODY[:20]):
                return [scores[n] for n in names] + [slack]
            for i, n in enumerate(names):
                if text == f"body of {n}":
                    v = [0.0] * (len(names) + 1)
                    v[i] = 1.0
                    return v
            return [0.0] * (len(names) + 1)
        return fake

    def candidates(self, scores: dict[str, float]) -> dict[str, str]:
        return {n: f"body of {n}" for n in scores}

    def test_it_matches_the_closest_above_the_threshold(self):
        from skillpp import similar
        scores = {"filing-a-bug": 0.90, "cutting-a-tag": 0.40}
        with unittest.mock.patch.object(similar, "embed",
                                        self.stub(scores)):
            best = similar.closest(self.BODY, self.candidates(scores))
        self.assertEqual(best.name, "filing-a-bug")
        self.assertTrue(best.confident)

    def test_a_near_miss_is_reported_but_not_confident(self):
        # The dangerous direction. A wrong merge inflates one count and discards
        # a proposal with nothing recording that it happened, so the caller has
        # to see the score rather than a boolean it cannot question.
        from skillpp import similar
        scores = {"filing-a-bug": similar.THRESHOLD - 0.01}
        with unittest.mock.patch.object(similar, "embed", self.stub(scores)):
            best = similar.closest(self.BODY, self.candidates(scores))
        self.assertEqual(best.name, "filing-a-bug")
        self.assertFalse(best.confident)

    def test_an_empty_store_matches_nothing(self):
        from skillpp import similar
        self.assertIsNone(similar.closest(self.BODY, {}))

    def test_recording_falls_through_to_new_when_ollama_is_down(self):
        """The safe direction, and it has to be this one.

        A duplicate entry can be merged later by a person. A wrong merge raises
        someone else's count and throws the proposal away.
        """
        import io
        from skillpp import similar
        from skillpp.cli import main
        transcript = write_transcript(
            self.root,
            *[rec(f"u{n}", content=f"request {n}") for n in range(30)],
            name="s.jsonl")
        memory.add_entry(self.config, name="already-here", body=self.BODY)
        with unittest.mock.patch.object(
                similar, "embed",
                side_effect=similar.EmbeddingUnavailable("not running")):
            with unittest.mock.patch("sys.stdin", io.StringIO(self.BODY)):
                with unittest.mock.patch.dict(os.environ,
                                              {"SKILLPP_MIN_NEW": "1"}):
                    code = main(["--root", str(self.config.root),
                                 "record-candidate", str(transcript),
                                 "--name", "something-new", "--auto-match"])
        self.assertEqual(code, 0)
        names = sorted(e.name for e in memory.load(self.config))
        self.assertIn("something-new", names)
        self.assertEqual(memory.find(memory.load(self.config),
                                     "already-here").count, 0)


class TestTheStepTrace(TempRoot):
    """What actually ran, kept beside the body that generalises it.

    Taken from the competing branch, which retains a numbered trace per entry.
    Measured on four real sessions: its recall was never the problem, and the
    trace is the one artefact it had that this branch did not.

    Kept per *session* rather than per candidate. Per candidate was tried and
    gave three entries from one session the same 712 steps -- true of the
    session, useless about any of the three. Without segmentation the session
    is the honest granularity, and saying so is better than implying a
    precision that is not there.
    """

    STEPS = [{"tool": "Bash", "input": {"command": "uv run --with pytest pytest"}},
             {"tool": "Write", "input": {"file_path": "/tmp/x.md"}}]

    def test_the_trace_is_not_inside_the_entry_file(self):
        """A `## Steps` section would need splitting back out on read.

        An ambiguous delimiter has destroyed this store twice -- once on `##`,
        once on `---`. A separate file needs no delimiter at all.
        """
        memory.add_entry(self.config, name="filing", body="Intro.\n\n## Rule\n\nDo it.")
        memory.add_steps(self.config, session="s1", steps=self.STEPS)
        entry = (self.config.patterns_dir / "filing.md").read_text()
        self.assertNotIn("uv run", entry)
        self.assertIn("## Rule", memory.find(memory.load(self.config), "filing").body)

    def test_a_body_containing_a_steps_heading_still_round_trips(self):
        body = "Intro.\n\n## Steps\n\n1. `do the thing`\n\n## Rule\n\nDo it."
        memory.add_entry(self.config, name="filing", body=body)
        memory.add_steps(self.config, session="s1", steps=self.STEPS)
        self.assertEqual(memory.find(memory.load(self.config), "filing").body, body)

    def test_the_trace_is_written_once(self):
        path = memory.add_steps(self.config, session="s1", steps=self.STEPS)
        before = path.read_bytes()
        memory.add_steps(self.config, session="s1",
                         steps=[{"tool": "Bash", "input": {"command": "rm -rf /"}}])
        self.assertEqual(path.read_bytes(), before)

    def test_a_corrupt_line_does_not_lose_the_trace(self):
        memory.add_steps(self.config, session="s1", steps=self.STEPS)
        with memory.steps_path(self.config, "s1").open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        self.assertEqual(len(memory.read_steps(self.config, "s1")), 2)

    def test_the_trace_file_is_not_mistaken_for_an_entry(self):
        memory.add_entry(self.config, name="filing", body="b")
        memory.add_steps(self.config, session="s1", steps=self.STEPS)
        self.assertEqual([c.name for c in memory.load(self.config)], ["filing"])


class TestTraceIsScrubbedBeforeDisk(TempRoot):
    """Their capture's input whitelist, ported for the reason it exists.

    Storing raw tool input was tried and gave a 621 KB trace holding whole
    file bodies and both sides of every edit, unscrubbed. A credential in an
    edited file went to disk verbatim.
    """

    def make(self, name, **inp):
        from skillpp.transcript import Block, Message
        return Message(uuid="u1", role="assistant", timestamp="t",
                       blocks=(Block(kind="tool_use", name=name, tool_input=inp),))

    def test_a_write_keeps_its_path_and_drops_its_contents(self):
        from skillpp.prepare import trace
        secret = "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"
        steps = trace([self.make("Write", file_path="/tmp/x.sh", content=secret)])
        self.assertEqual(steps[0]["input"], {"file_path": "/tmp/x.sh"})

    def test_an_edit_keeps_neither_side_of_the_change(self):
        from skillpp.prepare import trace
        steps = trace([self.make("Edit", file_path="/tmp/x.py",
                                 old_string="token = 'abc'",
                                 new_string="token = 'def'")])
        self.assertEqual(steps[0]["input"], {"file_path": "/tmp/x.py"})

    def test_a_command_is_kept_because_the_argument_is_the_step(self):
        from skillpp.prepare import trace
        steps = trace([self.make("Bash", command="uv run --with pytest pytest")])
        self.assertIn("uv run", steps[0]["input"]["command"])

    def test_a_long_argument_is_truncated(self):
        from skillpp.prepare import trace
        steps = trace([self.make("Bash", command="echo " + "x" * 5000)],
                      max_chars=100)
        self.assertLess(len(steps[0]["input"]["command"]), 200)

    def test_a_read_contributes_nothing_but_its_name(self):
        from skillpp.prepare import trace
        steps = trace([self.make("Read", file_path="/tmp/secrets.env")])
        self.assertEqual(steps[0], {"tool": "Read", "input": {}})


class TestRedraft(TempRoot):
    """The reader for the trace, which was write-only until it existed."""

    def seed(self, body="Old body.\n\n## Rule\n\nDo it."):
        memory.add_entry(self.config, name="filing", body=body)
        memory.add_occurrence(self.config, name="filing", session="s1")
        memory.add_steps(self.config, session="s1", steps=[
            {"tool": "Bash", "input": {"command": "uv run --with pytest pytest"}},
            {"tool": "Read", "input": {}},
        ])

    def test_browsing_is_not_trace(self):
        """A step whose arguments were all dropped tells a redraft nothing.

        Counting them made 712 "steps" out of a session that ran 14 commands.
        """
        self.seed()
        entry = memory.find(memory.load(self.config), "filing")
        steps = memory.trace_for(self.config, entry)
        self.assertEqual([s["tool"] for s in steps], ["Bash"])

    def test_show_needs_no_model(self):
        from skillpp.cli import main
        self.seed()
        code = main(["--root", str(self.config.root), "redraft", "filing", "--show"])
        self.assertEqual(code, 0)

    def test_an_entry_with_no_trace_refuses_rather_than_guessing(self):
        """Entries recorded before traces were kept cannot be redrafted.

        Refusing says so; redrafting from the body alone would be the model
        rewriting its own prose with nothing new to go on.
        """
        from skillpp.cli import main
        memory.add_entry(self.config, name="old", body="b")
        memory.add_occurrence(self.config, name="old", session="s0")
        self.assertEqual(
            main(["--root", str(self.config.root), "redraft", "old", "--apply"]), 1)

    def test_the_body_is_taken_from_between_the_markers(self):
        import subprocess
        from skillpp.cli import main
        self.seed()
        said = ("thinking out loud\n<<<SKILLPP-BODY\nNew body.\n\n## Probe first\n"
                "\nDo that.\nSKILLPP-BODY>>>\nAdds: probing first.")
        done = subprocess.CompletedProcess([], 0, stdout=said, stderr="")
        with unittest.mock.patch("subprocess.run", return_value=done):
            code = main(["--root", str(self.config.root), "redraft", "filing",
                         "--apply"])
        self.assertEqual(code, 0)
        draft = self.config.root / "drafts" / "filing" / "SKILL.md"
        self.assertEqual(draft.read_text().strip(),
                         "New body.\n\n## Probe first\n\nDo that.")
        # The store and the entry are untouched: this is a proposal.
        self.assertIn("Old body", memory.find(memory.load(self.config), "filing").body)

    def test_no_markers_means_the_body_was_already_right(self):
        import subprocess
        from skillpp.cli import main
        self.seed()
        done = subprocess.CompletedProcess([], 0, stdout="Already correct.", stderr="")
        with unittest.mock.patch("subprocess.run", return_value=done):
            main(["--root", str(self.config.root), "redraft", "filing", "--apply"])
        self.assertFalse((self.config.root / "drafts" / "filing" / "SKILL.md").exists())


class TestDerivedDependencies(TempRoot):
    """`skillpp check` reads requires_cli, and nothing ever wrote it."""

    def test_only_commands_count_not_prose(self):
        body = ("Run the suite.\n\n```\nuv run --with pytest pytest tests/\n```\n\n"
                "Then `git status --short`. Mention docker in passing only.")
        self.assertEqual(memory.requires_cli(body), ["git", "uv"])

    def test_embedded_code_does_not_become_a_dependency(self):
        """The reason this reads the body and not the raw trace.

        Deriving from captured commands produced `print(f\"`, `opt.step()` and
        `§4.2.3` as declared dependencies, because splitting on `;` walks into
        the Python inside `python3 -c`.
        """
        body = '```\npython3 -c "print(f\'x\'); opt.step(); print(1)"\n```'
        self.assertEqual(memory.requires_cli(body), ["python3"])

    def test_python_in_a_fence_is_not_a_list_of_programs(self):
        """A fenced block is not necessarily shell.

        Measured on a real body: a Python snippet declared `fixed`, `mock` and
        `original_shape` as commands to install.
        """
        body = "```\nfixed = compute(x)\nmock = Mock()\n```\n\n`python3 -m pytest`"
        self.assertEqual(memory.requires_cli(body), ["python3"])

    def test_a_repo_script_is_not_something_to_install(self):
        self.assertEqual(memory.requires_cli("```\n./scripts/run.sh\n```"), [])

    def test_an_environment_prefix_is_not_a_program(self):
        self.assertEqual(memory.requires_cli("```\nPYTHONPATH=. pytest -q\n```"), [])


class TestAutoMatchWithinASession(TempRoot):
    """A second candidate from the same session is not a second sighting.

    Reproduces a real failure: two distinct procedures recorded from one
    multi-task session, the second call auto-merged into the first at a score
    the log reported as 0.603 -- below every threshold in similar.py. The
    session comparison, not the body comparison, made the call.

    `nearest_session` is handed `prepared.new_messages` joined -- the whole
    session's new text, not the individual candidate. Both `record-candidate`
    calls in one `/log-session` run embed the *same* text, because the
    session's messages do not change between them. So the second call is
    compared against an exemplar this same call sequence just wrote from
    identical text, and it cannot help but match -- independent of whether the
    two procedures share anything at all.
    """

    def test_a_second_candidate_from_the_same_session_stays_separate(self):
        import io
        from skillpp import similar
        from skillpp.cli import main

        transcript = write_transcript(
            self.root,
            *[rec(f"u{n}", content=f"step {n}") for n in range(30)],
            name="two-chores.jsonl")

        # Session text is identical across both calls, exactly as the real CLI
        # produces it -- that collision is the point. Body text differs, so the
        # mock has to tell them apart or a body-comparison collision would mask
        # whether the session fix actually worked.
        def fake_embed(text, **_kw):
            if "weekly" in text:
                return [0.0, 1.0, 0.0]
            if "expenses" in text:
                return [0.0, 0.0, 1.0]
            return [1.0, 0.0, 0.0]  # the (identical) session text itself

        with unittest.mock.patch.object(similar, "embed",
                                        side_effect=fake_embed):
            with unittest.mock.patch("sys.stdin",
                                     io.StringIO("Draft the weekly update.")):
                with unittest.mock.patch.dict(os.environ,
                                              {"SKILLPP_MIN_NEW": "1"}):
                    code = main(["--root", str(self.config.root),
                                 "record-candidate", str(transcript),
                                 "--name", "drafting-weekly-update",
                                 "--auto-match"])
            self.assertEqual(code, 0)

            with unittest.mock.patch("sys.stdin",
                                     io.StringIO("File the month's expenses.")):
                with unittest.mock.patch.dict(os.environ,
                                              {"SKILLPP_MIN_NEW": "1"}):
                    code = main(["--root", str(self.config.root),
                                 "record-candidate", str(transcript),
                                 "--name", "filing-expenses",
                                 "--auto-match"])
            self.assertEqual(code, 0)

        names = sorted(e.name for e in memory.load(self.config))
        self.assertEqual(names, ["drafting-weekly-update", "filing-expenses"],
                         "the second candidate was merged into the first "
                         "purely because they came from the same session")


class TestSessionExemplars(TempRoot):
    """Matching a session against previous sessions, not against a body.

    Measured on real data: a session sits near-equidistant from every written
    body in the store — a spread of 0.07 across all of them — so there is
    nothing to choose between. Two sessions doing the same procedure separate:
    0.727 to 0.827 against 0.638 to 0.723 for different ones.

    That gap is 0.004 wide, which is why there are three zones rather than a
    threshold. The middle one decides nothing and is handed on, and these tests
    exist mostly to hold that boundary in place.
    """

    def vec(self, *values: float) -> list[float]:
        return list(values)

    def test_exemplars_are_appended_never_rewritten(self):
        from skillpp import similar
        with unittest.mock.patch.object(similar, "embed",
                                        return_value=[1.0, 0.0]):
            similar.add_exemplar(self.config, name="filing", session="s1",
                                 text="x")
            before = self.config.exemplars_file.read_bytes()
            similar.add_exemplar(self.config, name="filing", session="s2",
                                 text="y")
        after = self.config.exemplars_file.read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertEqual(len(similar.exemplars(self.config)), 2)

    def test_a_procedure_is_scored_on_its_best_sighting_not_its_average(self):
        """Averaging hides the match.

        The question is whether this session looks like *any* previous run of
        the work. One unusual sighting should not drag a good match below the
        line.
        """
        from skillpp import similar
        vectors = iter([[1.0, 0.0], [0.0, 1.0]])  # one aligned, one orthogonal
        with unittest.mock.patch.object(similar, "embed",
                                        side_effect=lambda *a, **k: next(vectors)):
            similar.add_exemplar(self.config, name="filing", session="s1", text="x")
            similar.add_exemplar(self.config, name="filing", session="s2", text="y")
        with unittest.mock.patch.object(similar, "embed",
                                        return_value=[1.0, 0.0]):
            near = similar.nearest_session(self.config, "a new session")
        self.assertEqual(near.name, "filing")
        self.assertAlmostEqual(near.score, 1.0, places=5)   # best, not 0.5
        self.assertEqual(near.sessions, 2)

    def test_the_three_zones(self):
        from skillpp.similar import Nearest, SURE_MATCH, SURE_NEW
        self.assertTrue(Nearest("a", SURE_MATCH, 1).matches)
        self.assertTrue(Nearest("a", SURE_NEW, 1).is_new)
        mid = (SURE_MATCH + SURE_NEW) / 2
        middle = Nearest("a", mid, 1)
        self.assertTrue(middle.unsure)
        self.assertFalse(middle.matches)
        self.assertFalse(middle.is_new)

    def test_the_measured_scores_land_where_they_should(self):
        """The boundary, pinned to the numbers it came from.

        Any edit that moves SURE_MATCH or SURE_NEW past these fails here, which
        is the point: they are not round numbers, they are the ends of a
        measured gap.
        """
        from skillpp.similar import Nearest
        # Highest score seen between two sessions doing *different* work.
        self.assertFalse(Nearest("a", 0.723, 1).matches)
        # Lowest between two doing the *same* work.
        self.assertFalse(Nearest("a", 0.727, 1).is_new)
        # Comfortably same, comfortably different.
        self.assertTrue(Nearest("a", 0.827, 1).matches)
        self.assertTrue(Nearest("a", 0.638, 1).is_new)

    def test_an_empty_log_matches_nothing(self):
        from skillpp import similar
        self.assertIsNone(similar.nearest_session(self.config, "anything"))

    def test_a_corrupt_line_does_not_lose_the_rest(self):
        from skillpp import similar
        with unittest.mock.patch.object(similar, "embed",
                                        return_value=[1.0, 0.0]):
            similar.add_exemplar(self.config, name="filing", session="s1", text="x")
        with self.config.exemplars_file.open("a", encoding="utf-8") as handle:
            handle.write("not json\n\n")
        self.assertEqual(len(similar.exemplars(self.config)), 1)


class TestHostPortability(TempRoot):
    """Nothing about which agent or model a developer uses is hardcoded.

    The distinction that matters, and it is not obvious: the skill body is
    written by whatever agent runs `/log-session`, which is a prompt in a
    markdown file rather than an API call. A Cursor user's model writes it in
    Cursor and skillpp never knows. There is nothing to configure there and
    hardcoding a model would be strictly worse.

    What *is* hardcoded-shaped is the unattended path — draining a queue means
    starting an agent that nobody asked for, and both the binary and its flags
    are host-specific.
    """

    def test_the_default_agent_carries_the_flag_that_stops_a_loop(self):
        # A spawned session ends, the hook enqueues it, the next drain picks it
        # up. Losing this flag from the default makes the queue refill itself.
        self.assertIn("--no-session-persistence",
                      Config(self.root / "a").agent_command)

    def test_the_agent_command_is_overridable(self):
        with unittest.mock.patch.dict(
                os.environ, {"SKILLPP_AGENT": "cursor-agent -q {prompt}"}):
            self.assertEqual(Config(self.root / "b").agent_command,
                             "cursor-agent -q {prompt}")

    def test_the_local_model_names_and_host_are_overridable(self):
        with unittest.mock.patch.dict(os.environ, {
                "SKILLPP_OLLAMA": "http://gpu-box:11434",
                "SKILLPP_LOCAL_MODEL": "llama3.1:8b",
                "SKILLPP_EMBED_MODEL": "mxbai-embed-large"}):
            config = Config(self.root / "c")
            self.assertEqual(config.ollama_url, "http://gpu-box:11434")
            self.assertEqual(config.local_model, "llama3.1:8b")
            self.assertEqual(config.embed_model, "mxbai-embed-large")

    def test_a_configured_host_reaches_both_endpoints(self):
        from skillpp import local, similar
        with unittest.mock.patch.dict(
                os.environ, {"SKILLPP_OLLAMA": "http://gpu-box:11434/"}):
            config = Config(self.root / "d")
        self.assertEqual(local.endpoint_for(config),
                         "http://gpu-box:11434/api/generate")
        self.assertEqual(similar.endpoint_for(config),
                         "http://gpu-box:11434/api/embed")

    def test_no_module_names_a_vendor_binary(self):
        """The guard on the thing this class exists for.

        `claude` may appear as a *default* in config, where a developer can
        override it. It must not appear in the modules that do the work, or the
        override is a lie.
        """
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / "skillpp"
        for module in sorted(root.glob("*.py")):
            if module.name == "config.py":
                continue
            text = module.read_text()
            with self.subTest(module.name):
                self.assertNotIn('"claude"', text)
                self.assertNotIn("'claude'", text)


class TestTheKnobsActuallyReachSomething(TempRoot):
    """Every documented override, tested through the code path it affects.

    Four of these were dead at once: `SKILLPP_RECURRENCE` never reached
    `reviewable`, `SKILLPP_LOCAL_MODEL` and `SKILLPP_EMBED_MODEL` were read into
    `Config` and never passed anywhere, and `SKILLPP_OLLAMA` had a helper —
    `endpoint_for` — that nothing called.

    The test that let that happen called `endpoint_for` directly and passed,
    which is the trap: a helper that works proves nothing about whether the code
    uses it. So these assert on the request that actually leaves the process,
    and on the value the caller actually gets.
    """

    def captured(self):
        """A urlopen stand-in that records the URL and body it was given."""
        seen = {}

        class Response:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner):
                return json.dumps({"response": "yes",
                                   "embeddings": [[1.0, 0.0]]}).encode()

        def fake(request, timeout=None):
            seen["url"] = request.full_url
            seen["body"] = json.loads(request.data.decode())
            return Response()
        return seen, fake

    def test_the_ollama_host_reaches_the_generate_request(self):
        from skillpp import local
        seen, fake = self.captured()
        with unittest.mock.patch.dict(
                os.environ, {"SKILLPP_OLLAMA": "http://gpu-box:9999"}):
            config = Config(self.root / "a")
        with unittest.mock.patch("urllib.request.urlopen", fake):
            local.triage("> do the thing\n  $ Bash make release\n    = ok\n" * 4,
                         model="some-model", config=config)
        self.assertEqual(seen["url"], "http://gpu-box:9999/api/generate")
        self.assertEqual(seen["body"]["model"], "some-model")

    def test_the_ollama_host_reaches_the_embed_request(self):
        from skillpp import similar
        seen, fake = self.captured()
        with unittest.mock.patch.dict(os.environ, {
                "SKILLPP_OLLAMA": "http://gpu-box:9999",
                "SKILLPP_EMBED_MODEL": "mxbai-embed-large"}):
            config = Config(self.root / "b")
        with unittest.mock.patch("urllib.request.urlopen", fake):
            similar.closest("a body", {"stored": "another body"}, config=config)
        self.assertEqual(seen["url"], "http://gpu-box:9999/api/embed")
        self.assertEqual(seen["body"]["model"], "mxbai-embed-large")

    def test_the_embed_model_reaches_an_exemplar(self):
        from skillpp import similar
        seen, fake = self.captured()
        with unittest.mock.patch.dict(
                os.environ, {"SKILLPP_EMBED_MODEL": "mxbai-embed-large"}):
            config = Config(self.root / "c")
        with unittest.mock.patch("urllib.request.urlopen", fake):
            similar.add_exemplar(config, name="n", session="s", text="t")
        self.assertEqual(seen["body"]["model"], "mxbai-embed-large")

    def test_the_recurrence_threshold_reaches_the_review_queue(self):
        entry = memory.Candidate(name="x", body="b",
                                 sessions=["a", "b", "c"], dates=["d"] * 3)
        with unittest.mock.patch.dict(os.environ, {"SKILLPP_RECURRENCE": "5"}):
            raised = Config(self.root / "d")
        self.assertEqual(memory.reviewable([entry], config=raised), [])
        with unittest.mock.patch.dict(os.environ, {"SKILLPP_RECURRENCE": "2"}):
            lowered = Config(self.root / "e")
        self.assertEqual(len(memory.reviewable([entry], config=lowered)), 1)
        # And the module default still applies when nothing is configured.
        self.assertEqual(len(memory.reviewable([entry])), 1)

    def test_every_documented_knob_is_read_by_config(self):
        """Guards the reverse mistake: documenting a variable nothing reads."""
        documented = ("SKILLPP_ROOT", "SKILLPP_SKILLS_DIR", "SKILLPP_MIN_NEW",
                      "SKILLPP_AGENT", "SKILLPP_OLLAMA", "SKILLPP_LOCAL_MODEL",
                      "SKILLPP_EMBED_MODEL", "SKILLPP_RECURRENCE")
        source = (Path(__file__).resolve().parent.parent
                  / "skillpp" / "config.py").read_text()
        for name in documented:
            with self.subTest(name):
                self.assertIn(name, source)


class TestRenderWithoutTheStore(TempRoot):
    """Omitting the recorded candidates from the prompt.

    They were there so the model could decide whether a proposal matched
    something already recorded. Since `--auto-match` that decision is an
    embedding and `log-session.md` tells the model not to make it, but the block
    stayed — measured on the real store, 53% of the prompt, growing with the
    store while the session does not.

    A flag rather than a deletion: the block shows several procedures with their
    full section structure immediately before the model is asked how many
    procedures are in *this* session, which is a plausible source of the
    run-to-run variance in that count. The flag exists so that can be tested
    rather than assumed.
    """

    def prepared(self):
        from skillpp.prepare import prepare
        records = []
        for n in range(MIN_NEW_MESSAGES + 2):
            records.append(rec(f"u{n}", content=f"request number {n}"))
        path = write_transcript(self.root, *records, name="s.jsonl")
        memory.add_entry(self.config, name="already-stored",
                         body="Do the thing.\n\n## A section\n\nDo it well.")
        memory.add_occurrence(self.config, name="already-stored", session="old")
        return prepare(str(path), config=self.config)

    def test_the_default_still_carries_the_store(self):
        from skillpp.prepare import render
        text = render(self.prepared())
        self.assertIn("# Candidates recorded so far", text)
        self.assertIn("already-stored", text)

    def test_no_store_omits_it(self):
        from skillpp.prepare import render
        text = render(self.prepared(), store=False)
        self.assertNotIn("# Candidates recorded so far", text)
        self.assertNotIn("already-stored", text)

    def test_no_store_keeps_the_session_and_the_header(self):
        # The point is to remove one block, not to shrink the prompt generally.
        # A flag that also dropped the work would make the experiment measure
        # two changes at once.
        from skillpp.prepare import render
        text = render(self.prepared(), store=False)
        self.assertIn("# New in this session", text)
        self.assertIn("request number 0", text)
        self.assertIn("conversation", text)
        self.assertIn("transcript", text)

    def test_the_environment_can_withhold_the_store(self):
        """A variable, not a flag, and the reason is the command template.

        `$ARGUMENTS` is substituted into every command a template runs, and
        `log-session.md` runs three. A `--no-store` passed that way reached
        `record-candidate` and `commit-session`, which reject it — so the eval
        arm meant to test anchoring measured argparse instead, twice banking
        zero candidates because the model correctly refused to run commands
        that fail.
        """
        with unittest.mock.patch.dict(os.environ, {"SKILLPP_NO_STORE": "1"}):
            self.assertFalse(Config(self.root / "x").include_store)
        self.assertTrue(Config(self.root / "y").include_store)

    def test_withholding_the_store_leaves_other_commands_alone(self):
        # The failure mode this replaced: a mechanism that reaches commands it
        # was never meant to touch.
        import contextlib
        import io
        from skillpp.cli import main
        records = [rec(f"u{n}", content=f"request {n}")
                   for n in range(MIN_NEW_MESSAGES + 2)]
        path = write_transcript(self.root, *records, name="e.jsonl")
        buf = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"SKILLPP_NO_STORE": "1",
                                                   "SKILLPP_MIN_NEW": "1"}):
            with unittest.mock.patch("sys.stdin", io.StringIO("a body\n\n## s\n\nrun")):
                with contextlib.redirect_stdout(buf):
                    code = main(["--root", str(self.config.root),
                                 "record-candidate", str(path), "--name", "ok"])
        self.assertEqual(code, 0, buf.getvalue())

    def test_a_session_below_the_floor_still_says_stop_either_way(self):
        # That message is not part of the store block and must survive, or arm B
        # would review sessions arm A refuses.
        from skillpp.prepare import prepare, render
        path = write_transcript(self.root, rec("u1", content="tiny"),
                                name="thin.jsonl")
        thin = prepare(str(path), config=self.config)
        for store in (True, False):
            with self.subTest(store=store):
                self.assertIn("Stop here", render(thin, store=store))
