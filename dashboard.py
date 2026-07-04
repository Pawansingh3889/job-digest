"""Live dashboard for the job pipeline. Standard library only, loopback only.

  python dashboard.py          # serves http://127.0.0.1:8765 and opens it
  python dashboard.py --quiet  # serve without opening the browser

Reads seen.db and applications/ on every poll, so whatever you do in the
terminal shows up here within three seconds. Suggests the next commands.
"""

import json
import sqlite3
import sys
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "seen.db"
PORT = 8765


def funnel():
    try:
        con = sqlite3.connect(DB)
        rows = con.execute(
            "SELECT slug, company, title, stage, substr(created,1,10), substr(updated,1,10), url "
            "FROM apps ORDER BY created"
        ).fetchall()
        con.close()
    except sqlite3.OperationalError:
        return []
    apps = []
    for n, (slug, company, title, stage, created, updated, url) in enumerate(rows, 1):
        folder = HERE / "applications" / slug
        apps.append({
            "n": n, "slug": slug, "company": company, "title": title,
            "stage": stage, "created": created, "updated": updated, "url": url,
            "has_letter": (folder / "cover-letter.md").exists(),
            "has_pdf": bool(list(folder.glob("CV_*.pdf"))),
            "has_tailor": (folder / "cv-draft.md").exists(),
        })
    return apps


def open_roles():
    try:
        con = sqlite3.connect(DB)
        run = con.execute("SELECT MAX(run_date) FROM shown").fetchone()[0]
        if not run:
            return run, []
        rows = con.execute(
            "SELECT s.url, s.score, seen.title, seen.company, seen.payload, substr(seen.first_seen,1,10) "
            "FROM shown s JOIN seen ON seen.url = s.url "
            "WHERE s.run_date = ? ORDER BY s.score DESC, s.url", (run,)
        ).fetchall()
        con.close()
    except sqlite3.OperationalError:
        return None, []
    today = datetime.now().strftime("%Y-%m-%d")
    roles = []
    for i, (url, score, title, company, payload, first_seen) in enumerate(rows, 1):
        source = ""
        if payload:
            try:
                source = json.loads(payload).get("source", "")
            except json.JSONDecodeError:
                pass
        roles.append({
            "i": i, "url": url, "score": score, "title": title,
            "company": company, "source": source, "new": first_seen == today,
        })
    return run, roles


def suggestions(apps, run, roles):
    tips = []
    picked_urls = {a["url"] for a in apps}
    for a in apps:
        if a["stage"] == "picked" and a["has_letter"] and a["has_pdf"]:
            tips.append({"cmd": f".\\apply fill {a['n']}",
                         "why": f"{a['company']}: pack complete, walk the form and submit"})
    for a in apps:
        if a["stage"] == "picked" and not a["has_tailor"]:
            tips.append({"cmd": f".\\apply tailor {a['n']}",
                         "why": f"{a['company']}: stage the CV draft and letter"})
    for r in roles:
        if r["url"] not in picked_urls and (r["score"] or 0) >= 50:
            tips.append({"cmd": f".\\apply pick {r['i']}",
                         "why": f"unpicked, score {r['score']}: {r['title']} at {r['company']}"})
    stale = []
    for a in apps:
        if a["stage"] == "applied":
            try:
                days = (datetime.now(timezone.utc) - datetime.fromisoformat(a["updated"] + "T00:00:00+00:00")).days
            except ValueError:
                days = 0
            if days >= 14:
                stale.append(a)
    for a in stale:
        tips.append({"cmd": "", "why": f"{a['company']}: applied {a['updated']}, silent 14+ days, send one follow-up"})
    if run != datetime.now().strftime("%Y-%m-%d"):
        tips.append({"cmd": ".\\digest", "why": "digest has not run today"})
    if not tips:
        tips.append({"cmd": ".\\apply status", "why": "queue clear; next digest at 07:30 or after logon"})
    return tips[:8]


def state():
    apps = funnel()
    run, roles = open_roles()
    latest = HERE / "digests" / "latest.html"
    last_write = datetime.fromtimestamp(latest.stat().st_mtime).strftime("%H:%M:%S") if latest.exists() else "never"
    return {
        "now": datetime.now().strftime("%H:%M:%S"),
        "digest_run": run, "digest_written": last_write,
        "apps": apps, "roles": roles,
        "suggestions": suggestions(apps, run, roles),
        "counts": {
            "open": len(roles),
            "picked": sum(1 for a in apps if a["stage"] == "picked"),
            "applied": sum(1 for a in apps if a["stage"] == "applied"),
            "interview": sum(1 for a in apps if a["stage"] == "interview"),
            "offer": sum(1 for a in apps if a["stage"] == "offer"),
            "rejected": sum(1 for a in apps if a["stage"] == "rejected"),
        },
    }


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>job-digest live</title>
<style>
  :root { --bg:#0a0e27; --panel:#10163a; --line:#1e2650; --text:#e8ecf8; --dim:#8b93b5; --cyan:#00d4ff; --amber:#e8a411; --green:#3ecf8e; --red:#ff5d5d; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--text); font:14px/1.45 "Cascadia Mono","Consolas",monospace; padding:22px; }
  h1 { font-size:17px; letter-spacing:.4px; }
  h2 { font-size:12px; color:var(--cyan); text-transform:uppercase; letter-spacing:1.2px; margin:22px 0 10px; }
  .pulse { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--cyan); margin-right:8px; animation:p 2s infinite; vertical-align:1px; }
  @keyframes p { 0%,100%{opacity:1} 50%{opacity:.25} }
  @media (prefers-reduced-motion: reduce) { .pulse { animation:none; } }
  .meta { color:var(--dim); font-size:12px; margin-top:4px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(330px,1fr)); gap:10px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:11px 13px; }
  .card a { color:var(--text); text-decoration:none; font-weight:600; }
  .card a:hover { color:var(--cyan); }
  .sub { color:var(--dim); font-size:12px; margin-top:3px; }
  .chip { display:inline-block; font-size:10px; border-radius:4px; padding:1px 7px; margin-left:6px; vertical-align:1px; }
  .c-new { background:var(--cyan); color:#04223a; } .c-prog { background:var(--amber); color:#3a2a04; }
  .c-ok { color:var(--green); border:1px solid var(--green); } .c-no { color:var(--dim); border:1px solid var(--line); }
  .stage-picked { border-left:3px solid var(--amber); } .stage-applied { border-left:3px solid var(--cyan); }
  .stage-interview { border-left:3px solid var(--green); } .stage-offer { border-left:3px solid var(--green); }
  .stage-rejected { border-left:3px solid var(--line); opacity:.55; }
  .sugg { display:flex; align-items:center; gap:10px; background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:9px 13px; margin-bottom:8px; }
  .sugg code { color:var(--cyan); background:#081027; border:1px solid var(--line); border-radius:5px; padding:3px 9px; cursor:pointer; white-space:nowrap; }
  .sugg code:hover { border-color:var(--cyan); }
  .sugg .why { color:var(--dim); font-size:12px; }
  .counts span { margin-right:16px; color:var(--dim); font-size:12px; }
  .counts b { color:var(--text); }
  .score { color:var(--cyan); }
  .copied { color:var(--green) !important; border-color:var(--green) !important; }
</style></head><body>
<h1><span class="pulse"></span>job-digest live</h1>
<div class="meta" id="meta">connecting...</div>
<div class="counts" id="counts"></div>
<h2>suggested next commands (click to copy)</h2>
<div id="sugg"></div>
<h2>funnel</h2>
<div class="cards" id="funnel"></div>
<h2>open roles</h2>
<div class="cards" id="roles"></div>
<script>
function esc(s){const d=document.createElement('div');d.textContent=s??'';return d.innerHTML}
async function tick(){
  let s; try { s = await (await fetch('/api/state')).json(); } catch(e){ document.getElementById('meta').textContent='server stopped'; return; }
  document.getElementById('meta').textContent = `updated ${s.now} · digest run ${s.digest_run??'never'} · file written ${s.digest_written}`;
  document.getElementById('counts').innerHTML = Object.entries(s.counts).map(([k,v])=>`<span>${k} <b>${v}</b></span>`).join('');
  document.getElementById('sugg').innerHTML = s.suggestions.map(t=>
    `<div class="sugg">${t.cmd?`<code onclick="copy(this)">${esc(t.cmd)}</code>`:''}<span class="why">${esc(t.why)}</span></div>`).join('');
  document.getElementById('funnel').innerHTML = s.apps.map(a=>
    `<div class="card stage-${esc(a.stage)}"><a href="${esc(a.url)}" target="_blank">${esc(a.title)}</a>
     <div class="sub">#${a.n} · ${esc(a.company)} · ${esc(a.stage)} · ${esc(a.updated)}</div>
     <div class="sub"><span class="chip ${a.has_pdf?'c-ok':'c-no'}">cv pdf</span>
     <span class="chip ${a.has_letter?'c-ok':'c-no'}">letter</span>
     <span class="chip ${a.has_tailor?'c-ok':'c-no'}">tailored</span></div></div>`).join('');
  document.getElementById('roles').innerHTML = s.roles.map(r=>
    `<div class="card"><a href="${esc(r.url)}" target="_blank">${esc(r.title)}</a>
     ${r.new?'<span class="chip c-new">new</span>':''}
     <div class="sub"><span class="score">[${r.score}]</span> #${r.i} · ${esc(r.company)} · ${esc(r.source)}</div></div>`).join('');
}
function copy(el){ navigator.clipboard.writeText(el.textContent).then(()=>{ el.classList.add('copied'); setTimeout(()=>el.classList.remove('copied'),700); }); }
tick(); setInterval(tick, 3000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/state":
            body = json.dumps(state()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the console quiet


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"dashboard live at {url}  (Ctrl+C stops it)")
    if "--quiet" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
