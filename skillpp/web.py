"""A local browser UI for the skill library and the ledger.

Deliberately dependency-free: `http.server` from the stdlib plus one
self-contained HTML page. The project's whole install story is "clone it and
run it", and a bundler would be the heaviest thing in the repo.

Binds to 127.0.0.1 only. It reads and writes real files under the skills
directory, so it is a local tool, never something to expose.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config
from .install import validate_for_upload
from .ledger import IGNORED_STATUSES, Ledger, STATUS_CANDIDATE, STATUS_IGNORED
from .lifecycle import reconcile, scan
from .signals import detect, effects

# Skill directory names we will read or write. Anything else is rejected
# outright rather than sanitised — this handler writes to disk.
_SAFE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _skill_path(skills_dir: Path, config: Config, name: str) -> Path | None:
    """Resolve a skill name to a file, refusing anything outside the roots."""
    if not _SAFE_NAME.match(name or ""):
        return None
    for root in (skills_dir, config.cold_dir, config.archive_dir):
        candidate = (root / name / "SKILL.md").resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def _step_label(step: dict, limit: int = 140) -> str:
    """One readable line per step.

    Captured commands can be whole heredocs. Collapsing newlines and clipping
    keeps a candidate skimmable — if the untruncated body matters, that is a
    sign the span is too big to be a recipe, which is itself worth seeing.
    """
    payload = step.get("input") or {}
    text = (payload.get("command") or payload.get("text")
            or payload.get("file_path") or step.get("tool") or "")
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + " …"


def collect_state(config: Config, skills_dir: Path) -> dict:
    """Everything the page renders, in one payload."""
    ledger = Ledger(config)
    entries = list(ledger.all())
    threshold = config.recurrence_threshold

    skills = []
    for info in scan(skills_dir, config):
        skills.append({
            "name": info.name,
            "tier": info.tier,
            "uses": info.uses,
            "last_used": info.last_used,
            "provenance": info.provenance,
            "requires_cli": info.requires_cli,
            "requires_mcp": info.requires_mcp,
            "stale": info.stale_refs,
        })

    def entry_row(entry) -> dict:
        return {
            "id": entry.id,
            "title": entry.title,
            "status": entry.status,
            "source": entry.source,
            "occurrences": entry.occurrences,
            "last_seen": entry.last_seen,
            "notes": entry.notes,
            "steps": [_step_label(s) for s in entry.steps[:12]],
            "step_count": len(entry.steps),
            "questions": [q.text for q in detect(entry)[: config.max_questions]],
            "destructive": effects(entry.steps)["destructive"][:5],
            "recurrences_since_ignored": entry.recurrences_since_ignored,
            "ignore_looks_wrong": entry.ignore_looks_wrong(threshold),
            "ready": entry.ready(threshold),
        }

    drift = reconcile(ledger, skills_dir, config)
    return {
        "skills_dir": str(skills_dir),
        "ledger_dir": str(config.ledger_dir),
        "threshold": threshold,
        "skills": sorted(skills, key=lambda s: (s["tier"] != "hot", s["name"])),
        "candidates": [entry_row(e) for e in entries
                       if e.status == STATUS_CANDIDATE],
        "ignored": [entry_row(e) for e in entries if e.status in IGNORED_STATUSES],
        "promoted": [entry_row(e) for e in entries if e.status == "promoted"],
        "drift": [{"id": e.id, "title": e.title, "path": e.skill_path}
                  for e in drift["missing"]],
    }


def save_skill(path: Path, text: str) -> dict:
    """Write a skill back, keeping a timestamped backup and validating first."""
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    problems = validate_for_upload(tmp)
    if problems:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "problems": problems}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    shutil.copy2(path, path.with_suffix(f".md.bak-{stamp}"))
    tmp.replace(path)
    return {"ok": True, "problems": []}


def apply_entry_action(config: Config, entry_id: str, action: str) -> dict:
    ledger = Ledger(config)
    entry = ledger.get(entry_id)
    if entry is None:
        return {"ok": False, "error": "no such entry"}
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if action == "ignore":
        entry.status = STATUS_IGNORED
        entry.ignored_at = now
        entry.ignored_at_occurrences = entry.occurrences
    elif action == "reopen":
        entry.status = STATUS_CANDIDATE
        entry.skill_path = ""
        entry.notes = entry.notes or "Reopened from the web UI."
    else:
        return {"ok": False, "error": f"unknown action: {action}"}
    ledger.save(entry)
    return {"ok": True, "status": entry.status}


def make_handler(config: Config, skills_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # noqa: A003 - quieten the default logger
            pass

        def _send(self, payload, status=200, content_type="application/json"):
            body = (payload if isinstance(payload, bytes)
                    else json.dumps(payload).encode("utf-8"))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - stdlib naming
            route = urlparse(self.path)
            if route.path in ("/", "/index.html"):
                return self._send(PAGE.encode("utf-8"), content_type="text/html; charset=utf-8")
            if route.path == "/api/state":
                return self._send(collect_state(config, skills_dir))
            if route.path == "/api/skill":
                name = (parse_qs(route.query).get("name") or [""])[0]
                path = _skill_path(skills_dir, config, name)
                if path is None:
                    return self._send({"error": "not found"}, 404)
                return self._send({"name": name, "text": path.read_text(encoding="utf-8")})
            return self._send({"error": "not found"}, 404)

        def do_POST(self):  # noqa: N802
            route = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send({"error": "bad request"}, 400)

            if route.path == "/api/skill":
                path = _skill_path(skills_dir, config, data.get("name", ""))
                if path is None:
                    return self._send({"error": "not found"}, 404)
                return self._send(save_skill(path, data.get("text", "")))
            if route.path == "/api/entry":
                return self._send(apply_entry_action(
                    config, data.get("id", ""), data.get("action", "")))
            return self._send({"error": "not found"}, 404)

    return Handler


def serve(config: Config, skills_dir: Path, port: int = 8765,
          open_browser: bool = True) -> ThreadingHTTPServer:
    config.ensure_dirs()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(config, skills_dir))
    if open_browser:
        threading.Timer(0.4, webbrowser.open,
                        args=[f"http://127.0.0.1:{httpd.server_port}/"]).start()
    return httpd


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Skill Plus Plus</title>
<style>
:root{
  --bg:#fbfaf8; --panel:#fff; --ink:#1b1a18; --muted:#6c6862; --line:#e6e2dc;
  --accent:#9a5b3d; --warn:#b4552d; --ok:#4a7c59; --code:#f4f1ec;
}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --bg:#17161a; --panel:#1f1e23; --ink:#eceaf0; --muted:#9a95a3; --line:#312f38;
  --accent:#d99a72; --warn:#e0855c; --ok:#8fc9a0; --code:#26242c;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;
  align-items:baseline;gap:16px;flex-wrap:wrap}
h1{font-size:17px;margin:0;font-weight:650;letter-spacing:-.01em}
.paths{color:var(--muted);font-size:12px;font-family:ui-monospace,monospace}
.tabs{display:flex;gap:4px;padding:12px 24px 0}
.tab{padding:7px 14px;border:1px solid transparent;border-radius:7px 7px 0 0;
  background:none;color:var(--muted);cursor:pointer;font:inherit;font-size:14px}
.tab[aria-selected=true]{background:var(--panel);border-color:var(--line);
  border-bottom-color:var(--panel);color:var(--ink);font-weight:600}
main{padding:0 24px 40px}
.wrap{background:var(--panel);border:1px solid var(--line);border-radius:0 10px 10px 10px;
  padding:18px;min-height:60vh}
.split{display:grid;grid-template-columns:minmax(240px,320px) 1fr;gap:18px}
@media(max-width:820px){.split{grid-template-columns:1fr}}
ul{list-style:none;margin:0;padding:0}
li.item{padding:10px 12px;border:1px solid var(--line);border-radius:8px;
  margin-bottom:8px;cursor:pointer;background:var(--bg)}
li.item[aria-current=true]{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.name{font-weight:600}
.meta{color:var(--muted);font-size:12.5px;margin-top:3px}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:99px;
  border:1px solid var(--line);color:var(--muted);margin-right:5px}
.tag.hot{border-color:var(--ok);color:var(--ok)}
.tag.warn{border-color:var(--warn);color:var(--warn)}
textarea{width:100%;min-height:62vh;font:13px/1.5 ui-monospace,monospace;
  background:var(--code);color:var(--ink);border:1px solid var(--line);
  border-radius:8px;padding:12px;resize:vertical}
.row{display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap}
button.act{font:inherit;font-size:13px;padding:7px 14px;border-radius:7px;
  border:1px solid var(--line);background:var(--bg);color:var(--ink);cursor:pointer}
button.act.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.act:disabled{opacity:.5;cursor:default}
.msg{font-size:13px}.msg.err{color:var(--warn)}.msg.ok{color:var(--ok)}
.empty{color:var(--muted);padding:28px 0;text-align:center}
code{background:var(--code);padding:1px 5px;border-radius:4px;
  font:12px ui-monospace,monospace;word-break:break-all}
.steps{margin:8px 0 0;padding-left:0}
.steps li{color:var(--muted);font:12px/1.45 ui-monospace,monospace;padding:2px 0;
  white-space:pre-wrap;overflow-wrap:anywhere}
.more{color:var(--muted);font-size:12px;font-style:italic;padding-top:2px}
.q{color:var(--accent);font-size:13px;margin-top:6px}
.banner{border:1px solid var(--warn);border-radius:8px;padding:10px 12px;
  margin-bottom:14px;font-size:13.5px;color:var(--warn)}
</style></head><body>
<header>
  <h1>Skill Plus Plus</h1>
  <span class="paths" id="paths"></span>
</header>
<div class="tabs" role="tablist">
  <button class="tab" role="tab" data-tab="skills">Skills</button>
  <button class="tab" role="tab" data-tab="candidates">Candidates</button>
  <button class="tab" role="tab" data-tab="ignored">Ignored</button>
</div>
<main><div class="wrap" id="wrap"></div></main>
<script>
let S=null, tab='skills', sel=null, dirty=false;

const esc = s => String(s??'').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function load(){
  S = await (await fetch('/api/state')).json();
  document.getElementById('paths').textContent = S.skills_dir;
  render();
}

function render(){
  document.querySelectorAll('.tab').forEach(b =>
    b.setAttribute('aria-selected', b.dataset.tab===tab));
  const w = document.getElementById('wrap');
  if(tab==='skills') return renderSkills(w);
  if(tab==='candidates') return renderEntries(w, S.candidates, 'candidate');
  return renderEntries(w, S.ignored, 'ignored');
}

function renderSkills(w){
  if(!S.skills.length){
    w.innerHTML = '<p class="empty">No skills yet. Create one with '
      + '<code>/skillpp-new</code> or <code>/skillpp-keep</code>.</p>';
    return;
  }
  const drift = S.drift.length
    ? `<div class="banner">${S.drift.length} promoted skill(s) missing from disk.
       Their workflows stay invisible until you reopen or ignore them —
       <code>skillpp reconcile</code>.</div>` : '';
  w.innerHTML = drift + '<div class="split"><ul id="list"></ul><div id="pane"></div></div>';
  const list = document.getElementById('list');
  list.innerHTML = S.skills.map(s => `
    <li class="item" data-name="${esc(s.name)}" aria-current="${s.name===sel}">
      <div class="name">${esc(s.name)}</div>
      <div class="meta">
        <span class="tag ${s.tier==='hot'?'hot':''}">${esc(s.tier)}</span>
        <span class="tag">${s.uses} use${s.uses===1?'':'s'}</span>
        ${s.stale.length?`<span class="tag warn">${s.stale.length} stale ref</span>`:''}
      </div>
    </li>`).join('');
  list.querySelectorAll('.item').forEach(el =>
    el.onclick = () => openSkill(el.dataset.name));
  if(sel) openSkill(sel); else
    document.getElementById('pane').innerHTML =
      '<p class="empty">Pick a skill to view or edit it.</p>';
}

async function openSkill(name){
  if(dirty && !confirm('Discard unsaved changes?')) return;
  sel = name; dirty = false;
  document.querySelectorAll('#list .item').forEach(el =>
    el.setAttribute('aria-current', el.dataset.name===name));
  const d = await (await fetch('/api/skill?name='+encodeURIComponent(name))).json();
  const s = S.skills.find(x => x.name===name) || {stale:[],requires_cli:[]};
  document.getElementById('pane').innerHTML = `
    <textarea id="ed" spellcheck="false"></textarea>
    <div class="row">
      <button class="act primary" id="save" disabled>Save</button>
      <span class="msg" id="msg"></span>
    </div>
    ${s.stale.length ? `<div class="meta">Unresolved: ${
       s.stale.map(r=>`<code>${esc(r)}</code>`).join(' ')}</div>` : ''}
    ${s.requires_cli.length ? `<div class="meta">Requires: ${
       s.requires_cli.map(r=>`<code>${esc(r)}</code>`).join(' ')}</div>` : ''}`;
  const ed = document.getElementById('ed'), save = document.getElementById('save');
  ed.value = d.text || '';
  ed.oninput = () => { dirty = true; save.disabled = false;
                       document.getElementById('msg').textContent = ''; };
  save.onclick = async () => {
    save.disabled = true;
    const r = await (await fetch('/api/skill', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, text: ed.value})})).json();
    const msg = document.getElementById('msg');
    if(r.ok){ dirty=false; msg.className='msg ok'; msg.textContent='Saved.'; load(); }
    else { save.disabled=false; msg.className='msg err';
           msg.textContent = (r.problems||[r.error]).join(' · '); }
  };
}

function renderEntries(w, rows, kind){
  if(!rows.length){
    w.innerHTML = `<p class="empty">Nothing ${kind==='ignored'?'ignored':'waiting'}.</p>`;
    return;
  }
  w.innerHTML = '<ul>' + rows.map(e => `
    <li class="item" aria-current="false">
      <div class="name">${esc(e.title||e.id)}</div>
      <div class="meta">
        <span class="tag">×${e.occurrences}</span>
        <span class="tag">${esc(e.source)}</span>
        ${e.ready?'<span class="tag hot">ready</span>':''}
        ${e.ignore_looks_wrong?`<span class="tag warn">${
          e.recurrences_since_ignored}× since ignored</span>`:''}
        ${e.destructive.length?'<span class="tag warn">destructive</span>':''}
        <code>${esc(e.id)}</code>
      </div>
      <ul class="steps">${e.steps.map(s=>`<li>${esc(s)}</li>`).join('')}
        ${e.step_count>e.steps.length
          ? `<li class="more">+${e.step_count-e.steps.length} more steps —
             a span this long is probably not one recipe</li>` : ''}</ul>
      ${e.questions.map(q=>`<div class="q">? ${esc(q)}</div>`).join('')}
      ${e.notes?`<div class="meta">${esc(e.notes)}</div>`:''}
      <div class="row">
        ${kind==='candidate'
          ? `<button class="act" data-act="ignore" data-id="${esc(e.id)}">Ignore</button>`
          : `<button class="act" data-act="reopen" data-id="${esc(e.id)}">Reopen</button>`}
        <span class="meta">Promote with <code>/skillpp-review</code></span>
      </div>
    </li>`).join('') + '</ul>';
  w.querySelectorAll('button[data-act]').forEach(b => b.onclick = async () => {
    b.disabled = true;
    await fetch('/api/entry', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id:b.dataset.id, action:b.dataset.act})});
    load();
  });
}

document.querySelectorAll('.tab').forEach(b => b.onclick = () => {
  if(dirty && !confirm('Discard unsaved changes?')) return;
  dirty=false; tab=b.dataset.tab; render();
});
addEventListener('beforeunload', e => { if(dirty){ e.preventDefault(); e.returnValue=''; } });
load();
</script></body></html>
"""
