"""Tiering and decay for the skill library.

Approved skills are never auto-deleted (README 6). Disuse is a poor proxy for
value — the incident runbook is rare *by nature* — so unused skills are
demoted out of the always-loaded index, not destroyed:

    hot (indexed) → cold (searchable) → archived (explicit lookup only)

Because Claude Code indexes everything under the skills directory, demotion is
implemented as a file move rather than a flag (README 8).

Staleness is a separate signal from disuse: a skill rots when the script it
calls is renamed or the flag it passes is removed. That is detected by
checking whether its references still resolve.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Config

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_PROGRAM_RE = re.compile(r"\A[a-zA-Z][\w.-]*\Z")

# Sections that discuss the workflow rather than performing it. Scanning them
# would report a command named in a question as a broken reference.
_META_SECTIONS = {"known gaps", "judgement", "judgment", "notes", "when to use"}

# Shell builtins resolve to nothing on PATH but are never missing.
_SHELL_BUILTINS = {
    "export", "cd", "echo", "source", "alias", "set", "unset", "eval", "exec",
    "read", "shift", "test", "true", "false", "trap", "wait", "local", "return",
    "if", "then", "else", "fi", "for", "while", "do", "done", "case", "esac",
}


@dataclass
class SkillInfo:
    name: str
    path: Path
    tier: str
    requires_cli: list[str]
    requires_mcp: list[str]
    provenance: str
    stale_refs: list[str]

    @property
    def is_stale(self) -> bool:
        return bool(self.stale_refs)


def parse_frontmatter(text: str) -> dict:
    """Minimal YAML reader for the subset skills actually use.

    Scalars, inline JSON-ish lists, and one level of nesting under ``metadata``.
    Deliberately not a general YAML parser — this only has to read what
    :func:`skillpp.summary.scaffold_skill` writes, plus hand-authored skills
    that stick to the common shape.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    out: dict = {}
    current_parent: str | None = None
    for raw_line in match.group(1).splitlines():
        if not raw_line.strip() or raw_line.strip().startswith("#"):
            continue
        indented = raw_line[:1].isspace()
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        parsed: object
        if not value:
            parsed = {}
        elif value.startswith("["):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = [v.strip().strip("\"'") for v in value.strip("[]").split(",") if v.strip()]
        elif value.startswith(('"', "'")):
            parsed = value[1:-1]
        else:
            parsed = value
        if indented and current_parent:
            parent = out.setdefault(current_parent, {})
            if isinstance(parent, dict):
                parent[key] = parsed
        else:
            out[key] = parsed
            current_parent = key if parsed == {} else None
    return out


def _referenced(text: str, body_only: bool = True) -> tuple[list[str], list[str]]:
    """Extract referenced file paths and programs from a skill body.

    Reads the first token of every backticked span, so ``./scripts/deploy.sh
    prod`` yields the script rather than being skipped for containing a space.
    Meta sections are ignored — a command named inside an open question is not
    a reference to check.
    """
    body = _FRONTMATTER_RE.sub("", text) if body_only else text
    paths: list[str] = []
    programs: list[str] = []
    skipping = False

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("# ").strip().lower()
            skipping = heading in _META_SECTIONS
            continue
        if skipping:
            continue
        for match in _BACKTICK_RE.finditer(line):
            tokens = match.group(1).strip().split()
            if not tokens:
                continue
            ref = tokens[0].rstrip(".,;:")
            if not ref or ref.startswith(("http://", "https://", "${", "-", "$")):
                continue
            if "/" in ref or ref.startswith("."):
                paths.append(ref)
            elif _PROGRAM_RE.match(ref) and ref not in _SHELL_BUILTINS:
                programs.append(ref)

    return list(dict.fromkeys(paths)), list(dict.fromkeys(programs))


def check_staleness(skill_path: Path, project_root: Path | None = None) -> list[str]:
    """References that no longer resolve — the real decay signal."""
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError:
        return []
    root = project_root or Path.cwd()
    paths, programs = _referenced(text)
    stale: list[str] = []
    for ref in paths:
        if "${" in ref or ref.startswith("~"):
            continue
        # Only judge things that look like in-repo files, not prose with dots.
        if "/" not in ref and not ref.startswith("."):
            continue
        candidate = (root / ref) if not ref.startswith("/") else Path(ref)
        sibling = skill_path.parent / ref
        if not candidate.exists() and not sibling.exists():
            stale.append(f"path: {ref}")
    for program in programs:
        if shutil.which(program) is None and not (root / program).exists():
            stale.append(f"command: {program}")
    return stale


def scan(skills_dir: Path, config: Config,
         project_root: Path | None = None) -> list[SkillInfo]:
    """Inventory every skill across all tiers."""
    found: list[SkillInfo] = []
    tiers = [("hot", skills_dir), ("cold", config.cold_dir), ("archived", config.archive_dir)]
    for tier, directory in tiers:
        if not directory.exists():
            continue
        for skill_file in sorted(directory.glob("*/SKILL.md")):
            text = ""
            try:
                text = skill_file.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = parse_frontmatter(text)
            meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
            name = str(fm.get("name") or skill_file.parent.name)
            found.append(SkillInfo(
                name=name,
                path=skill_file,
                tier=tier,
                requires_cli=list(meta.get("requires_cli") or []),
                requires_mcp=list(meta.get("requires_mcp") or []),
                provenance=str(meta.get("provenance") or ""),
                stale_refs=check_staleness(skill_file, project_root),
            ))
    return found


# Moved here from `summary.py` when the ledger half was removed. It is
# about skills on disk, which is what the rest of this module is about;
# it only lived beside the proposal renderer because that is where the
# ledger happened to call it from.
def check_dependencies(deps_cli: list[str], deps_mcp: list[str],
                       cwd: Path | None = None) -> dict:
    """Dependency check at pull time, not at run time (README 5).

    Failing at install is cheap; failing halfway through a deploy is not.
    """
    cwd = Path(cwd or Path.cwd())
    missing_cli = [d for d in deps_cli if shutil.which(d) is None]

    configured: set[str] = set()
    for candidate in (cwd / ".mcp.json", Path.home() / ".claude.json",
                      cwd / ".claude" / "settings.json"):
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        servers = data.get("mcpServers") or {}
        if isinstance(servers, dict):
            configured.update(servers.keys())

    missing_mcp = []
    for tool in deps_mcp:
        parts = tool.split("__")
        server = parts[1] if len(parts) > 2 else tool
        if server not in configured:
            missing_mcp.append(tool)

    return {
        "ok": not missing_cli and not missing_mcp,
        "missing_cli": missing_cli,
        "missing_mcp": missing_mcp,
        "known_servers": sorted(configured),
    }


def reconcile(config: Config, skills_dir: Path) -> dict:
    """Report drift between promoted entries and the skill files on disk.

    **Reports only — never changes status.** A promoted entry whose skill file
    has been deleted is a real dead end: it stays out of the review queue while
    the file it points at is gone. Reopening it automatically would second-guess
    a deletion that was almost certainly deliberate, and re-propose the same
    procedure every time the developer declined. Deciding is theirs.

    Rewritten for the memory store rather than ported. It used to read the
    ledger and match on a `provenance: ledger:<id>` string written into each
    skill's frontmatter; `decisions.jsonl` already records `skill_path` on the
    promotion itself, so the link is stored once by the thing that created it
    instead of being reconstructed from two places that could disagree.
    """
    from .memory import PROMOTED, load

    result: dict[str, list] = {"ok": [], "missing": [], "unlinked": [],
                               "orphaned": []}
    promoted = [e for e in load(config) if e.status == PROMOTED]
    claimed = set()

    for entry in promoted:
        if not entry.skill_path:
            # Promoted, but the decision never recorded where it was written.
            result["unlinked"].append(entry)
            continue
        path = Path(entry.skill_path)
        claimed.add(path.parent.name)
        (result["ok"] if path.is_file() else result["missing"]).append(entry)

    # A skill this tool wrote whose entry is no longer promoted -- or gone.
    for skill in scan(skills_dir, config):
        if skill.name in claimed:
            continue
        if str(skill.provenance).startswith("skill-plus-plus") or \
                (skill.path.parent / "SKILL.md").is_file():
            if not any(e.name == skill.name for e in promoted):
                result["orphaned"].append(skill)

    return result


def move_tier(skill: SkillInfo, target_tier: str, skills_dir: Path,
              config: Config) -> Path:
    """Move a skill between hot / cold / archived. Never deletes."""
    destinations = {
        "hot": skills_dir,
        "cold": config.cold_dir,
        "archived": config.archive_dir,
    }
    if target_tier not in destinations:
        raise ValueError(f"unknown tier: {target_tier}")
    dest_root = destinations[target_tier]
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / skill.path.parent.name
    if dest.resolve() == skill.path.parent.resolve():
        return dest
    if dest.exists():
        shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
    shutil.move(str(skill.path.parent), str(dest))
    return dest
