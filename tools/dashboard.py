#!/usr/bin/env python3
"""Job listing dashboard: build a static page, or serve one with live marking.

The dashboard renders the normalized feed from `tools/feed.py`. Marking a
listing as applied never writes tracker state directly: it calls the existing
nudge pipeline (`ams_nudge.py set <id> <status>`), which stays the single writer
of `applications.json` and `applications.md`.

CLI
  python3 -B tools/dashboard.py build                  write DASHBOARD.html
  python3 -B tools/dashboard.py serve --port 8766      serve with POST /api/mark
  python3 -B tools/dashboard.py mark <id> applied      mark one listing
  python3 -B tools/dashboard.py mark <id> applied "note"

Marking paths, in the order the page tries them:
  1. inside the Hermes desktop chat frame: send the mark request as a prompt
  2. served by this tool: POST /api/mark, which persists through the pipeline
  3. opened from disk: copy the exact command for the terminal

Python 3 standard library only. Bind to 127.0.0.1 by default.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import feed as feedmod  # noqa: E402  (local module, same directory)
import generate as generatemod  # noqa: E402  (same directory)
import ui as uimod  # noqa: E402  (shared structure renderer for the local-only live view)

DASHBOARD = os.path.join(ROOT, "DASHBOARD.html")
LAUNCHER = os.path.join(ROOT, "ui", "jobhunter")
TRACKER_SCRIPT = os.path.abspath(os.environ.get(
    "JOBHUNTER_TRACKER_SCRIPT", os.path.join(ROOT, "ams", "ams_nudge.py")))
MARKABLE = ["applied", "interview", "blocked", "skipped", "nudged",
            "rejected", "success", "unclear"]
# Stages are the process truth and the coarse statuses are derived from them,
# so both spellings are accepted here. STAGE_ORDER lives in feed.py.
STAGES = list(feedmod.STAGE_ORDER)
MARKABLE_ALL = MARKABLE + [stage for stage in STAGES if stage not in MARKABLE]
TERMINAL_UPDATE = 30  # listings shown per status column in the static page


class DashboardError(Exception):
    pass


def _tracker(verb, job_id, value="", note=""):
    """Run one tracker subcommand and return the completed process."""
    command = [sys.executable, TRACKER_SCRIPT, verb, job_id]
    if value:
        command.append(value)
    if note:
        command.append(note)
    return subprocess.run(command, capture_output=True, text=True, timeout=60)


def _remote_tracker(verb, job_id, value="", note=""):
    """Run one tracker subcommand through the launcher's scoped remote writer.

    The launcher owns the SSH hop (``jobhunter-remote``), so the verb names are
    the tracker's own: ``stage``, ``set``, ``track``, ``bookmark``.
    """
    if os.environ.get("JOBUI_REMOTE_WRITES") != "1":
        # Only the interactive TUI opts into the remote writer; a direct call
        # without that explicit opt-in keeps failing closed.
        raise DashboardError("tracker script not found at %s" % TRACKER_SCRIPT)
    command = [LAUNCHER, "jobhunter-remote", verb, job_id]
    if value:
        command.append(value)
    if note:
        command.append(note)
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)


def remote_state():
    """The tracker's application records, read over the scoped launcher hop.

    Read only, and gated exactly like a write: nothing is asked of the server
    unless the interactive menu opted in, so a bare CLI call cannot reach it.
    """
    if os.environ.get("JOBUI_REMOTE_WRITES") != "1":
        raise DashboardError("tracker script not found at %s" % TRACKER_SCRIPT)
    command = [LAUNCHER, "jobhunter-remote", "state"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise DashboardError("remote tracker rejected the state read: %s"
                             % (result.stderr.strip() or result.stdout.strip()))
    try:
        payload = json.loads(result.stdout or "{}")
    except ValueError as exc:
        raise DashboardError("remote tracker sent no readable state: %s" % exc)
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    return jobs if isinstance(jobs, dict) else {}


def default_runner(job_id, status, note=""):
    """Persist a stage (or a raw status) through the nudge pipeline.

    Every button in the terminal writes through here, so an untracked listing
    is seeded with ``track`` first instead of failing the click.
    """
    verb = "stage" if status in feedmod.STAGE_STATUS else "set"
    remote = not os.path.exists(TRACKER_SCRIPT)
    if remote:
        result = _remote_tracker(verb, job_id, status, note)
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 and "unknown job" in output:
            _remote_tracker("track", job_id)
            result = _remote_tracker(verb, job_id, status, note)
            output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            raise DashboardError("remote tracker rejected the update: %s"
                                 % (result.stderr.strip() or result.stdout.strip()))
        return (result.stdout or "").strip()
    result = _tracker(verb, job_id, status, note)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 and "unknown job" in output:
        _tracker("track", job_id)
        result = _tracker(verb, job_id, status, note)
        output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise DashboardError("tracker rejected the update: %s"
                             % (result.stderr.strip() or result.stdout.strip()))
    return (result.stdout or "").strip()


def mark(job_id, status, note="", runner=default_runner):
    """Validate and apply one status change."""
    job_id = (job_id or "").strip()
    status = (status or "").strip().lower()
    if not job_id:
        raise DashboardError("job id is empty")
    if status not in MARKABLE_ALL:
        raise DashboardError("%r is neither a status nor a stage (allowed: %s)"
                             % (status, ", ".join(MARKABLE_ALL)))
    return runner(job_id, status, note)


def _esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def _score_text(score):
    return "" if score is None else ("%.2f" % score)


def _short_source(source):
    """Human label for where a score came from."""
    text = (source or "").strip()
    if not text:
        return ""
    if text.startswith("scored_jobs"):
        return "latest scan"
    if "historical" in text or text.startswith("state"):
        return "older scan"
    return text


CSS = """
:root{--bg:#0f1115;--panel:#171a21;--ink:#e8eaed;--muted:#9aa3b2;--line:#262b35;
--accent:#7cc4ff;--ok:#5ddb9a;--warn:#ffcf6b;--bad:#ff8f8f;--chip:#1d222c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
header{padding:18px 22px 12px;border-bottom:1px solid var(--line)}
h1{margin:0 0 4px;font-size:19px;font-weight:650;letter-spacing:0}
.sub{color:var(--muted);font-size:12.5px}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:7px 11px;font-size:12.5px}
.stat b{font-size:15px;display:block;margin-top:1px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:12px 22px;
border-bottom:1px solid var(--line);background:var(--panel)}
input[type=search],select{background:var(--bg);color:var(--ink);border:1px solid var(--line);
border-radius:7px;padding:7px 10px;font-size:13px}
input[type=search]{min-width:250px}
button{background:var(--chip);color:var(--ink);border:1px solid var(--line);
border-radius:7px;padding:6px 10px;font-size:12.5px;cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
button.on{border-color:var(--accent);color:var(--accent)}
button.go{border-color:#2f6b4f;color:var(--ok)}
main{padding:0 22px 40px}
table{width:100%;border-collapse:collapse;margin-top:14px;font-size:13px}
th,td{text-align:left;padding:8px 9px;border-bottom:1px solid var(--line);
vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;
position:sticky;top:0;background:var(--bg);cursor:pointer;white-space:nowrap}
tr:hover td{background:#141821}
.title{font-weight:600}
.meta{color:var(--muted);font-size:11.5px;margin-top:2px}
.score{font-variant-numeric:tabular-nums;font-weight:600}
.pill{display:inline-block;padding:2px 7px;border-radius:999px;font-size:11px;
border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.pill.applied{color:var(--ok);border-color:#2f6b4f}
.pill.interview{color:var(--accent);border-color:#2b5c80}
.pill.blocked{color:var(--bad);border-color:#6b3030}
.pill.nudged{color:var(--warn);border-color:#6b5a2a}
.pill.new{color:var(--muted)}
.pill.skipped,.pill.expired{color:#6f7683}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.actions{display:flex;flex-wrap:wrap;gap:5px;max-width:170px}
.actions button{padding:4px 7px;font-size:11.5px}
.empty{padding:40px;text-align:center;color:var(--muted)}
footer{padding:14px 22px;color:var(--muted);font-size:11.5px;border-top:1px solid var(--line)}
.toast{position:fixed;right:18px;bottom:18px;background:var(--panel);border:1px solid var(--accent);
color:var(--ink);padding:10px 14px;border-radius:9px;font-size:12.5px;max-width:420px;
opacity:0;transition:opacity .15s;pointer-events:none;z-index:9}
.toast.show{opacity:1}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px}
"""

JS = """
const JOBS = __DATA__;
const MARKABLE = __MARKABLE__;
let sortKey = 'score', sortDir = -1, statuses = new Set(), query = '', scanOnly = false;

function statusLabel(s){return JOBS.statusLabels[s]||s;}

function filtered(){
  const q = query.trim().toLowerCase();
  let rows = JOBS.jobs.filter(j => {
    if (statuses.size && !statuses.has(j.status)) return false;
    if (scanOnly && !j.in_latest_scan) return false;
    if (!q) return true;
    return (j.title+' '+j.company+' '+j.location+' '+(j.roles||[]).join(' ')+' '+
            (j.skills||[]).join(' ')).toLowerCase().includes(q);
  });
  rows.sort((a,b)=>{
    let av,bv;
    if (sortKey==='score'){av=a.score===null?-1:a.score;bv=b.score===null?-1:b.score;}
    else if (sortKey==='status'){av=a.status;bv=b.status;}
    else if (sortKey==='company'){av=a.company.toLowerCase();bv=b.company.toLowerCase();}
    else {av=a.title.toLowerCase();bv=b.title.toLowerCase();}
    if (av<bv) return -1*sortDir; if (av>bv) return 1*sortDir; return 0;
  });
  return rows;
}

function render(){
  const rows = filtered();
  const tbody = document.getElementById('rows');
  document.getElementById('shown').textContent = rows.length;
  if (!rows.length){tbody.innerHTML = '<tr><td colspan="5"><div class="empty">No listings match.</div></td></tr>';return;}
  tbody.innerHTML = rows.map(j => {
    const url = j.url ? '<a href="'+esc(j.url)+'" target="_blank" rel="noopener">AMS</a>' : '';
    const meta = [j.location, j.company, j.posted_date ? 'posted '+j.posted_date : '',
                  j.in_latest_scan ? '' : 'not in latest scan',
                  j.score_source ? 'score: '+j.score_source : 'no score'].filter(Boolean).join(' | ');
    const acts = MARKABLE.map(s =>
      '<button data-id="'+j.id+'" data-status="'+s+'"'+(s==='applied'?' class="go"':'')+'>'+
      (s==='nudged'?'undo':s)+'</button>').join('');
    return '<tr><td><div class="title">'+esc(j.title)+'</div><div class="meta">'+esc(meta)+
      '</div><div class="meta">'+url+(j.note?' | '+esc(j.note):'')+'</div></td>'+
      '<td><span class="pill '+j.status+'">'+statusLabel(j.status)+'</span></td>'+
      '<td class="score">'+(j.score===null?'-':j.score.toFixed(2))+'</td>'+
      '<td class="actions">'+acts+'</td><td class="meta" style="white-space:nowrap">'+esc(j.tracked_date||'')+'</td></tr>';
  }).join('');
  tbody.querySelectorAll('button[data-id]').forEach(b => {
    b.addEventListener('click', () => submitMark(b.dataset.id, b.dataset.status));
  });
}

function esc(s){return String(s==null?'':s).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

function toast(msg){const t=document.getElementById('toast');t.textContent=msg;
  t.classList.add('show');clearTimeout(window._tt);window._tt=setTimeout(()=>t.classList.remove('show'),6000);}

function submitMark(id, status){
  const job = JOBS.jobs.find(j => j.id===id);
  const label = statusLabel(status).toLowerCase();
  const cmd = 'python3 -B tools/dashboard.py mark '+id+' '+status;
  if (window.hermes && window.hermes.send){
    window.hermes.send('Mark job '+id+' ('+(job?job.title:'')+') as '+status+'.');
    toast('Sent to Hermes: mark as '+label+'.');
    return;
  }
  fetch('/api/mark',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:id,status:status})})
    .then(r=>r.json().then(b=>({ok:r.ok,body:b})))
    .then(({ok,body})=>{
      if(!ok){toast('Failed: '+(body.error||'unknown error'));return;}
      job.status = status; job.status_label = statusLabel(status);
      job.tracked_date = body.tracked_date || job.tracked_date;
      recount(); render(); toast('Marked '+(job.title||id)+' as '+label+'.');
    })
    .catch(()=>{toast('Not served: run '+cmd+' in the project folder.');});
}

function recount(){
  const c = {total:JOBS.jobs.length};
  JOBS.jobs.forEach(j => {c[j.status]=(c[j.status]||0)+1;});
  Object.keys(c).forEach(k => {const el=document.getElementById('c-'+k);
    if(el) el.textContent = c[k];});
}

function boot(){
  document.getElementById('c-total').textContent = JOBS.counts.total;
  ['new','nudged','applied','interview','blocked'].forEach(k=>{
    const el=document.getElementById('c-'+k); if(el) el.textContent = JOBS.counts[k]||0;});
  document.getElementById('c-scan').textContent = JOBS.counts.in_latest_scan;
  document.getElementById('scan-info').textContent = JOBS.scanLine;
  document.querySelectorAll('button[data-filter]').forEach(b=>{
    b.addEventListener('click',()=>{
      const s=b.dataset.filter;
      if (s==='scan'){scanOnly=!scanOnly;b.classList.toggle('on',scanOnly);render();return;}
      if (statuses.has(s)){statuses.delete(s);b.classList.remove('on');}
      else {statuses.add(s);b.classList.add('on');}
      render();
    });
  });
  document.getElementById('q').addEventListener('input',e=>{query=e.target.value;render();});
  document.querySelectorAll('th[data-sort]').forEach(th=>{
    th.addEventListener('click',()=>{
      const k=th.dataset.sort;
      if (sortKey===k) sortDir*=-1; else {sortKey=k;sortDir=(k==='score')?-1:1;}
      render();
    });
  });
  render();
}
document.addEventListener('DOMContentLoaded', boot);
"""


def payload(feed):
    counts = feed.get("counts") or {}
    scan = feed.get("scan") or {}
    bits = []
    if scan.get("scraped_at"):
        bits.append("latest AMS scan %s" % scan["scraped_at"][:16].replace("T", " "))
    if scan.get("location"):
        bits.append(scan["location"])
    if scan.get("period"):
        bits.append(scan["period"])
    if scan.get("keyword_count"):
        bits.append("%s role keywords" % scan["keyword_count"])
    if scan.get("tracker_updated_at"):
        bits.append("tracker updated %s" % scan["tracker_updated_at"][:16].replace("T", " "))
    return {
        "counts": counts,
        "statusLabels": feedmod.STATUS_LABEL,
        "scanLine": " | ".join(bits) or "no scan metadata",
        "jobs": [
            {
                "id": job["id"],
                "title": job["title"],
                "company": job["company"],
                "location": job["location"],
                "url": job["url"],
                "score": job["score"],
                "score_source": _short_source(job["score_source"]),
                "posted_date": job["posted_date"],
                "in_latest_scan": job["in_latest_scan"],
                "status": job["status"],
                "tracked_date": (job["tracked_date"] or "")[:10],
                "note": job["note"],
                "roles": job["roles"],
                "skills": job["skills"],
            }
            for job in feed.get("jobs") or []
        ],
    }


def _packet_text(packet):
    """The review window's text, section by section, for the browser panel."""
    lines = []
    meta = packet.get("meta.json") or {}
    lines.append("Model %s - %s" % (meta.get("model") or "unknown",
                                    str(meta.get("generated_at") or "").replace("T", " ")[:16]))
    for title, name in (("Cover letter", "cover-letter.md"), ("CV notes", "cv-notes.md"),
                        ("Company", "company.md"),
                        ("Why this letter, why this channel", "rationale.md")):
        body = [line.strip() for line in (packet.get(name) or "").splitlines() if line.strip()]
        if not body:
            continue
        lines.append("")
        lines.append(title.upper())
        for line in body:
            lines.append("# " + line if line.startswith("#") else line)
    documents = packet.get("documents.json") or []
    if documents:
        lines.append("")
        lines.append("DOCUMENTS")
        for entry in documents:
            note_text = str(entry.get("note") or "")
            lines.append("- %s%s" % (entry.get("name") or "", " - " + note_text if note_text else ""))
    return "\n".join(lines)


def render_html(feed, mode="static"):
    data = payload(feed)
    counts = feed.get("counts") or {}
    served_note = ("Served mode: marking writes through the AMS nudge pipeline."
                   if mode == "served" else
                   "Static build: marking works inside the Hermes chat, or copy the command.")
    stats = [
        ("total", "Listings", counts.get("total", 0)),
        ("new", "New", counts.get("new", 0)),
        ("nudged", "Asked", counts.get("nudged", 0)),
        ("applied", "Applied", counts.get("applied", 0)),
        ("interview", "Interview", counts.get("interview", 0)),
        ("blocked", "Blocked", counts.get("blocked", 0)),
        ("scan", "In latest scan", counts.get("in_latest_scan", 0)),
    ]
    stat_html = "".join(
        '<div class="stat">%s<b id="c-%s">%s</b></div>' % (_esc(label), key, value)
        for key, label, value in stats)
    filters = "".join(
        '<button data-filter="%s">%s</button>' % (key, feedmod.STATUS_LABEL[key])
        for key in ("new", "nudged", "applied", "interview", "blocked"))
    warnings = "".join('<div class="sub">warning: %s</div>' % _esc(item)
                       for item in feed.get("warnings") or [])
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job listings dashboard</title>
<style>%(css)s</style></head>
<body>
<header>
  <h1>Job listings dashboard</h1>
  <div class="sub" id="scan-info"></div>
  %(warnings)s
  <div class="stats">%(stats)s</div>
</header>
<div class="bar">
  <input type="search" id="q" placeholder="Filter title, company, location, skill">
  %(filters)s
  <button data-filter="scan">In latest scan</button>
  <span class="sub"><b id="shown">0</b> shown</span>
</div>
<main>
<table>
  <thead><tr>
    <th data-sort="title">Listing</th>
    <th data-sort="status">Status</th>
    <th data-sort="score">Score</th>
    <th>Mark</th>
    <th>Last marked</th>
  </tr></thead>
  <tbody id="rows"></tbody>
</table>
</main>
<footer>%(note)s Data: AMS scan in career/ams (read-only). Tracker: ams_nudge.py.</footer>
<div class="toast" id="toast"></div>
<script>%(js)s</script>
</body></html>
""" % {
        "css": CSS,
        "stats": stat_html,
        "filters": filters,
        "warnings": warnings,
        "note": _esc(served_note),
        "js": JS.replace("__DATA__", _js_json(data))
               .replace("__MARKABLE__", _js_json(MARKABLE)),
    }


def _js_json(payload):
    """Serialize for inline <script> use: no character can close the tag or start markup."""
    return (json.dumps(payload, ensure_ascii=False)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def write_dashboard(feed, path=DASHBOARD):
    html_text = render_html(feed, mode="static")
    tmp = path + ".pending-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(html_text)
    os.replace(tmp, path)
    return path


def build(source_dir=feedmod.DEFAULT_SOURCE, path=DASHBOARD):
    feed = feedmod.build_feed(source_dir)
    write_dashboard(feed, path)
    return feed, path


class Handler(BaseHTTPRequestHandler):
    source_dir = feedmod.DEFAULT_SOURCE
    runner = staticmethod(default_runner)

    def bookmark_tracker(verb, job_id):      # noqa: N805  (class level default)
        return _tracker(verb, job_id)

    bookmark_tracker = staticmethod(bookmark_tracker)

    def log_message(self, format, *args):  # noqa: A002  (stdlib signature)
        return

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        blob = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        # Cache-busting queries (?v=...) are common in browser verification and
        # must not turn a real route into a 404.
        path = urlsplit(self.path).path
        if path in {"/", "/index.html", "/dashboard"}:
            feed = feedmod.build_feed(self.source_dir)
            self._send(200, render_html(feed, mode="served"), "text/html; charset=utf-8")
        elif path == "/gui.html":
            feed = feedmod.build_feed(self.source_dir)
            spec = uimod.load()
            ctx = uimod.overlay_tracker_state(uimod.context_from_feed(feed, mode="served"))
            self._send(200, uimod.render_gui(spec, ctx), "text/html; charset=utf-8")
        elif path == "/api/packet":
            query = urlsplit(self.path).query
            listing_id = ""
            for part in query.split("&"):
                if part.startswith("id="):
                    listing_id = unquote(part[3:])
            packet = generatemod.read_packet(generatemod.DEFAULT_OUT, listing_id)
            if not packet:
                self._send(404, json.dumps({"error": "no packet for this listing yet"}))
                return
            text = _packet_text(packet)
            self._send(200, json.dumps({"ok": True, "id": listing_id, "text": text,
                                        "meta": packet.get("meta.json") or {}},
                                       ensure_ascii=False))
        elif path.startswith("/api/feed"):
            feed = feedmod.build_feed(self.source_dir)
            self._send(200, json.dumps(feed, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        if route not in {"/api/mark", "/api/bookmark", "/api/note", "/api/generate",
                         "/api/unstage"}:
            self._send(404, json.dumps({"error": "not found"}))
            return
        # This server is loopback-only. Reject cross-site forms/fetch requests
        # and DNS rebinding before they can reach the one authorized writer.
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        origin = self.headers.get("Origin", "")
        allowed_hosts = {"127.0.0.1", "localhost"}
        public_host = os.environ.get("JOBHUNTER_DASHBOARD_HOST", "").strip().lower()
        if public_host:
            allowed_hosts.add(public_host)
        host_header = self.headers.get("Host", "")
        public_origin = os.environ.get("JOBHUNTER_DASHBOARD_ORIGIN", "").strip()
        expected_origin = public_origin if host == public_host and public_origin else "http://" + host_header
        if host not in allowed_hosts or (origin and origin != expected_origin):
            self._send(403, json.dumps({"error": "same-origin requests only"}))
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._send(415, json.dumps({"error": "JSON content type required"}))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 8192 or length < 0:
                self._send(413, json.dumps({"error": "request too large"}))
                return
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, OSError) as exc:
            self._send(400, json.dumps({"error": "bad request: %s" % exc}))
            return
        if route == "/api/note":
            try:
                output = note(str(body.get("id") or ""), str(body.get("note") or ""))
            except DashboardError as exc:
                self._send(400, json.dumps({"error": str(exc)}))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)}))
                return
            stored = ""
            try:
                stored = json.loads(output).get("note", "")
            except ValueError:
                stored = ""
            self._send(200, json.dumps({"ok": True, "id": body.get("id"),
                                        "note": stored or body.get("note")}))
            return
        if route == "/api/unstage":
            try:
                output = unstage(str(body.get("id") or ""))
            except DashboardError as exc:
                self._send(400, json.dumps({"error": str(exc)}))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)}))
                return
            restored = ""
            try:
                restored = str(json.loads(output).get("stage") or "")
            except ValueError:
                restored = ""
            self._send(200, json.dumps({"ok": True, "id": body.get("id"),
                                        "stage": restored, "output": output}))
            return
        if route == "/api/generate":
            force = bool(body.get("force"))
            try:
                options = {}
                if "generation_model" in body:
                    model = body["generation_model"]
                    if not isinstance(model, str) or model not in generatemod.GENERATION_MODELS:
                        raise DashboardError("unsupported generation model")
                    options["generation_model"] = model
                output = generate(str(body.get("id") or ""), force=force, **options)
            except DashboardError as exc:
                message = str(exc)
                already = "already exists" in message
                self._send(409 if already else 400,
                           json.dumps({"error": message, "needs_confirm": already}))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)}))
                return
            stage_updated = False
            stage_error = ""
            stored_stage = ""
            stored_status = ""
            try:
                job_id = str(body.get("id") or "")
                mark(job_id, "generated", runner=self.runner)
                if (not os.path.exists(TRACKER_SCRIPT)
                        and os.environ.get("JOBUI_REMOTE_WRITES") == "1"):
                    record = remote_state().get(job_id) or {}
                    stored_stage = str(record.get("stage") or "")
                    stored_status = str(record.get("status") or "")
                else:
                    feed = feedmod.build_feed(self.source_dir)
                    record = next((job for job in feed.get("jobs") or []
                                   if str(job.get("id") or "") == job_id), {})
                    stored_stage = str(record.get("stage") or "")
                    stored_status = str(record.get("status") or "")
                stage_updated = stored_stage == "generated"
                if not stage_updated:
                    stage_error = "tracker read-back did not confirm generated"
            except Exception as exc:  # packet succeeded; keep that result distinct from a failed stage write
                stage_error = str(exc)
            self._send(200, json.dumps({
                "ok": True,
                "packet_written": True,
                "id": body.get("id"),
                "output": output[-400:],
                "stage_updated": stage_updated,
                "stored_stage": stored_stage,
                "stored_status": stored_status,
                "stage_label": feedmod.STAGE_LABEL.get(stored_stage, stored_stage),
                "stage_error": stage_error,
            }))
            return
        if route == "/api/bookmark":
            try:
                output = bookmark(body.get("id"), tracker=self.bookmark_tracker)
            except DashboardError as exc:
                self._send(400, json.dumps({"error": str(exc)}))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)}))
                return
            feed = feedmod.build_feed(self.source_dir)
            job = next((item for item in feed["jobs"] if item["id"] == body.get("id")), None)
            stored = bool((job or {}).get("bookmarked"))
            self._send(200, json.dumps({"ok": True, "id": body.get("id"),
                                        "bookmarked": stored, "output": output}))
            return
        try:
            output = mark(body.get("id"), body.get("status"), body.get("note") or "",
                          runner=self.runner)
        except DashboardError as exc:
            self._send(400, json.dumps({"error": str(exc)}))
            return
        except Exception as exc:  # noqa: BLE001  (a failed write must not kill the server)
            self._send(500, json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)}))
            return
        feed = feedmod.build_feed(self.source_dir)
        job = next((item for item in feed["jobs"] if item["id"] == body.get("id")), None)
        # The feed describes the scan; the write went to the tracker, which is
        # the only place that knows the stage the button just stored.
        record = (uimod._tracker_jobs() or {}).get(str(body.get("id") or "")) or {}
        if record.get("stage"):
            job = dict(job or {}, stage=record.get("stage"),
                       status=record.get("status") or (job or {}).get("status"))
        requested = (body.get("status") or "").strip().lower()
        stored_stage = (job or {}).get("stage") or ""
        stored_status = (job or {}).get("status") or ""
        # A stage write has to be confirmed as a stage: the coarse status behind
        # it ("new" for unread, "applied" for sent) is a derived label and says
        # nothing about which button was pressed.
        stored = stored_stage if requested in feedmod.STAGE_STATUS else stored_status
        self._send(200, json.dumps({
            "ok": stored == requested,
            "id": body.get("id"),
            "status": requested,
            "previous_status": stored_status,
            "stored_stage": stored_stage,
            "stored_status": stored_status,
            "tracked_date": (job or {}).get("tracked_date", ""),
            "output": output,
        }))


def serve(source_dir=feedmod.DEFAULT_SOURCE, host="127.0.0.1", port=8766, runner=None,
          bookmark_tracker=None):
    handler = type("BoundHandler", (Handler,), {"source_dir": source_dir})
    if runner is not None:
        handler.runner = staticmethod(runner)
    if bookmark_tracker is not None:
        handler.bookmark_tracker = staticmethod(bookmark_tracker)
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd


def cmd_build(argv):
    parser = argparse.ArgumentParser(prog="dashboard.py build")
    parser.add_argument("--source", default=feedmod.DEFAULT_SOURCE)
    parser.add_argument("--out", default=DASHBOARD)
    args = parser.parse_args(argv)
    feed, path = build(args.source, args.out)
    counts = feed["counts"]
    print("OK dashboard=%s listings=%d new=%d applied=%d"
          % (os.path.relpath(path, ROOT), counts["total"], counts["new"],
             counts["applied"]))
    return 0


def bookmark(job_id, tracker=None):
    """Flip the bookmark on one listing through the same single writer.

    ``tracker`` is injectable so the loopback server and its tests never reach
    the real tracker unless a runner is wired in on purpose.
    """
    run = tracker or _tracker
    if tracker is None and not os.path.exists(TRACKER_SCRIPT):
        result = _remote_tracker("bookmark", job_id)
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 and "unknown job" in output:
            _remote_tracker("track", job_id)
            result = _remote_tracker("bookmark", job_id)
            output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            raise DashboardError("remote tracker rejected the bookmark: %s"
                                 % (result.stderr.strip() or result.stdout.strip()))
        return (result.stdout or "").strip()
    result = run("bookmark", job_id)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 and "unknown job" in output:
        run("track", job_id)
        result = run("bookmark", job_id)
        output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise DashboardError("tracker rejected the bookmark: %s"
                             % (result.stderr.strip() or result.stdout.strip()))
    return (result.stdout or "").strip()


def cmd_bookmark(argv):
    global TRACKER_SCRIPT
    parser = argparse.ArgumentParser(prog="dashboard.py bookmark")
    parser.add_argument("job_id")
    parser.add_argument("--tracker", default=TRACKER_SCRIPT)
    args = parser.parse_args(argv)
    TRACKER_SCRIPT = args.tracker
    output = bookmark(args.job_id)
    state = "on"
    try:
        state = "on" if json.loads(output).get("bookmarked") else "off"
    except Exception:
        pass
    print("OK bookmark %s for %s" % (state, args.job_id))
    if output:
        print(output)
    return 0


def unstage(job_id):
    """Step one application back to the stage it held before the last move."""
    remote = not os.path.exists(TRACKER_SCRIPT)
    result = (_remote_tracker("unstage", job_id) if remote
              else _tracker("unstage", job_id))
    if result.returncode:
        where = "remote tracker" if remote else "tracker"
        output = (result.stderr or result.stdout or "").strip()
        raise DashboardError("%s could not undo the stage: %s"
                             % (where, output.splitlines()[0] if output else "failed"))
    return (result.stdout or "").strip()


def note(job_id, text):
    """Store one application's feedback note without moving its stage."""
    text = (text or "").strip()
    if not text:
        raise DashboardError("the note is empty")
    remote = not os.path.exists(TRACKER_SCRIPT)
    for attempt in (1, 2):
        result = (_remote_tracker("note", job_id, "", text) if remote
                  else _tracker("note", job_id, "", text))
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0:
            return (result.stdout or "").strip()
        if attempt == 1 and "unknown job" in output:
            _remote_tracker("track", job_id) if remote else _tracker("track", job_id)
            continue
        where = "remote tracker" if remote else "tracker"
        raise DashboardError("%s rejected the note: %s"
                             % (where, result.stderr.strip() or result.stdout.strip()))
    return ""


GENERATE_TIMEOUT = 1200


def generate(job_id, force=False, language=generatemod.DEFAULT_LANGUAGE, generation_model=None):
    """Write one application packet, on whichever host can reach the model.

    The Mac client has no engine of its own, so the packet is written on the
    server and carried back by the same Syncthing folder the repo rides on.
    """
    if language not in generatemod.GENERATION_LANGUAGES:
        raise DashboardError("unsupported generation language")
    args = [job_id, "--language", language]
    if generation_model is not None:
        if not isinstance(generation_model, str) or generation_model not in generatemod.GENERATION_MODELS:
            raise DashboardError("unsupported generation model")
        args += ["--generation-model", generation_model]
    if force:
        args.append("--force")
    if shutil.which("hermes"):
        command = [sys.executable, "-B", os.path.join(ROOT, "tools", "generate.py")] + args
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                timeout=GENERATE_TIMEOUT)
    else:
        if os.environ.get("JOBUI_REMOTE_WRITES") != "1":
            raise DashboardError("no model engine on this host; the remote writer "
                                 "must be opted in with JOBUI_REMOTE_WRITES=1")
        result = subprocess.run([LAUNCHER, "jobhunter-remote", "generate"] + args,
                                cwd=ROOT, capture_output=True, text=True,
                                timeout=GENERATE_TIMEOUT)
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        first = output.splitlines()[0] if output else "exit %s" % result.returncode
        raise DashboardError("the generator failed: %s" % first)
    return output


def cmd_packet(argv):
    """Print one application packet as the review window reads it."""
    parser = argparse.ArgumentParser(prog="dashboard.py packet")
    parser.add_argument("job_id")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--local", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not generatemod.ID_RE.fullmatch(args.job_id):
        raise DashboardError("not a listing id")
    if args.json and not args.local and os.environ.get("JOBUI_REMOTE_WRITES") == "1" \
            and not shutil.which("hermes"):
        result = subprocess.run([LAUNCHER, "jobhunter-remote", "packet", args.job_id],
                                cwd=ROOT, capture_output=True, text=True, timeout=15)
        if result.returncode != 0 and result.returncode != 4:
            raise DashboardError((result.stderr or "packet request failed").strip())
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode
    packet = generatemod.read_packet(generatemod.DEFAULT_OUT, args.job_id)
    if not packet:
        print("no packet for %s yet" % args.job_id, file=sys.stderr)
        return 4 if args.json else 1
    print(json.dumps(packet, ensure_ascii=False) if args.json else _packet_text(packet))
    return 0


def cmd_generate(argv):
    parser = argparse.ArgumentParser(prog="dashboard.py generate")
    parser.add_argument("job_id")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--language", choices=generatemod.GENERATION_LANGUAGES,
                        default=generatemod.DEFAULT_LANGUAGE)
    parser.add_argument("--generation-model", choices=generatemod.GENERATION_MODELS)
    args = parser.parse_args(argv)
    output = generate(args.job_id, force=args.force, language=args.language,
                      generation_model=args.generation_model)
    print("OK generated %s" % args.job_id)
    if output:
        print(output)
    return 0


def cmd_unstage(argv):
    parser = argparse.ArgumentParser(prog="dashboard.py unstage")
    parser.add_argument("job_id")
    args = parser.parse_args(argv)
    print(unstage(args.job_id))
    return 0


def cmd_note(argv):
    parser = argparse.ArgumentParser(prog="dashboard.py note")
    parser.add_argument("job_id")
    parser.add_argument("note", nargs="+")
    args = parser.parse_args(argv)
    output = note(args.job_id, " ".join(args.note))
    print("OK noted %s" % args.job_id)
    if output:
        print(output)
    return 0


def cmd_mark(argv):
    global TRACKER_SCRIPT
    parser = argparse.ArgumentParser(prog="dashboard.py mark")
    parser.add_argument("job_id")
    parser.add_argument("status")
    parser.add_argument("note", nargs="?", default="")
    parser.add_argument("--tracker", default=TRACKER_SCRIPT)
    args = parser.parse_args(argv)
    TRACKER_SCRIPT = args.tracker
    output = mark(args.job_id, args.status, args.note)
    print("OK marked %s as %s" % (args.job_id, args.status))
    if output:
        print(output)
    return 0


def cmd_serve(argv):
    parser = argparse.ArgumentParser(prog="dashboard.py serve")
    parser.add_argument("--source", default=feedmod.DEFAULT_SOURCE)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    httpd = serve(args.source, args.host, args.port)
    print("OK serving http://%s:%d/ (ctrl-c to stop)" % (args.host, args.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        httpd.server_close()
    return 0


COMMANDS = {"build": cmd_build, "mark": cmd_mark, "bookmark": cmd_bookmark,
            "unstage": cmd_unstage, "note": cmd_note,
            "generate": cmd_generate, "packet": cmd_packet,
            "serve": cmd_serve}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__.strip())
        return 0
    command = COMMANDS.get(argv[0])
    if command is None:
        print("unknown command %r" % argv[0], file=sys.stderr)
        return 2
    try:
        return command(argv[1:])
    except DashboardError as exc:
        print("FAIL: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
