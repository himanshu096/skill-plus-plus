"""Unit tests. Stdlib only: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import os
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
from skillpp.lifecycle import check_staleness, parse_frontmatter, record_use, scan
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
