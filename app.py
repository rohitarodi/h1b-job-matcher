"""
H1B Job Matcher — Web Server
----------------------------
Flask app that serves a live dashboard.
• Runs the matcher on startup and every hour in a background thread.
• POST /api/reload triggers an immediate refresh.
• GET  /api/status  returns cache state (is_running, last_updated, counts).
• GET  /api/jobs    returns the full matched-jobs JSON array.
"""

import threading
import time
import sys
from datetime import datetime

from flask import Flask, jsonify, request

from h1b_matcher import run_matcher, GITHUB_SOURCES, MATCH_THRESHOLD

app = Flask(__name__)

# ── Shared cache ──────────────────────────────────────────────────────────────
_cache = {
    "jobs":         [],
    "last_updated": None,   # ISO string
    "is_running":   False,
    "error":        None,
    "next_refresh":  None,  # ISO string
}
_lock = threading.Lock()

REFRESH_INTERVAL = 3600   # seconds (1 hour)


# ── Background worker ─────────────────────────────────────────────────────────

def _do_run():
    """Run the matcher and update the cache. Thread-safe."""
    with _lock:
        if _cache["is_running"]:
            return          # already running — skip
        _cache["is_running"] = True
        _cache["error"] = None

    print(f"[{datetime.now():%H:%M:%S}] Starting matcher run …", flush=True)
    try:
        jobs = run_matcher()
        with _lock:
            _cache["jobs"] = jobs
            _cache["last_updated"] = datetime.now().isoformat()
            _cache["error"] = None
        print(f"[{datetime.now():%H:%M:%S}] Done — {len(jobs)} matched jobs.", flush=True)
    except Exception as exc:
        with _lock:
            _cache["error"] = str(exc)
        print(f"[{datetime.now():%H:%M:%S}] ERROR: {exc}", flush=True)
    finally:
        with _lock:
            _cache["is_running"] = False
            _cache["next_refresh"] = datetime.fromtimestamp(
                time.time() + REFRESH_INTERVAL
            ).isoformat()


def _scheduler():
    """Run the matcher on startup, then every REFRESH_INTERVAL seconds."""
    while True:
        _do_run()
        time.sleep(REFRESH_INTERVAL)


threading.Thread(target=_scheduler, daemon=True).start()


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    with _lock:
        return jsonify({
            "is_running":   _cache["is_running"],
            "last_updated": _cache["last_updated"],
            "next_refresh": _cache["next_refresh"],
            "job_count":    len(_cache["jobs"]),
            "error":        _cache["error"],
        })


@app.get("/api/jobs")
def api_jobs():
    with _lock:
        return jsonify(_cache["jobs"])


@app.post("/api/reload")
def api_reload():
    with _lock:
        if _cache["is_running"]:
            return jsonify({"message": "Already running"}), 409
    threading.Thread(target=_do_run, daemon=True).start()
    return jsonify({"message": "Reload started"})


# ── Frontend (self-contained SPA) ─────────────────────────────────────────────

_SOURCE_COLORS = {
    "speedyapply/2026-SWE-College-Jobs": "#2563eb",
    "vanshb03/New-Grad-2026":            "#7c3aed",
}
_SOURCE_OPTIONS = "".join(
    f'<option value="{s["name"]}">{s["name"]}</option>'
    for s in GITHUB_SOURCES
)
_SOURCE_LEGEND = " &nbsp;|&nbsp; ".join(
    f'<a href="{s["repo_url"]}" target="_blank">{s["name"]}</a>'
    for s in GITHUB_SOURCES
)

import json as _json
_SRC_COLORS_JSON = _json.dumps(_SOURCE_COLORS)

_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>H1B Job Matcher</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, sans-serif; background: #f8fafc; color: #1e293b;
         margin: 0; padding: 1.25rem; }}
  a {{ color: inherit; }}
  h1 {{ font-size: 1.6rem; margin: 0 0 .2rem; }}
  h2 {{ font-size: 1.1rem; font-weight: 700; margin: 0 0 .75rem; }}

  /* Top bar */
  .topbar {{ display: flex; align-items: center; flex-wrap: wrap;
             gap: .75rem; margin-bottom: .4rem; }}
  .meta {{ color: #64748b; font-size: .82rem; flex: 1; }}
  .reload-btn {{ display: inline-flex; align-items: center; gap: .4rem;
                 background: #2563eb; color: #fff; border: none; cursor: pointer;
                 padding: .45rem 1rem; border-radius: .4rem; font-size: .85rem;
                 font-weight: 600; transition: background .15s; }}
  .reload-btn:hover:not(:disabled) {{ background: #1d4ed8; }}
  .reload-btn:disabled {{ background: #93c5fd; cursor: not-allowed; }}
  .spinner {{ width: 14px; height: 14px; border: 2px solid #fff;
              border-top-color: transparent; border-radius: 50%;
              animation: spin .6s linear infinite; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .next-refresh {{ font-size: .75rem; color: #94a3b8; margin-bottom: 1rem; }}

  /* Status bar */
  .status-bar {{ display: flex; align-items: center; gap: .5rem;
                 font-size: .8rem; color: #64748b; margin-bottom: 1.25rem; }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; background: #94a3b8; }}
  .dot.live  {{ background: #16a34a; }}
  .dot.spin  {{ background: #f59e0b; animation: pulse 1s ease-in-out infinite; }}
  .dot.error {{ background: #dc2626; }}
  @keyframes pulse {{ 0%,100% {{ opacity:1 }} 50% {{ opacity:.4 }} }}

  /* Stats */
  .stats {{ display: flex; gap: .85rem; flex-wrap: wrap; margin-bottom: 1.5rem; }}
  .stat {{ background: #fff; border: 1px solid #e2e8f0; border-radius: .5rem;
           padding: .65rem 1.1rem; }}
  .stat-num {{ font-size: 1.4rem; font-weight: 700; }}
  .stat-lbl {{ font-size: .7rem; color: #64748b; text-transform: uppercase;
               letter-spacing: .05em; }}

  /* Sponsoring companies */
  .sponsors-section {{ background: #fff; border: 1px solid #e2e8f0; border-radius: .75rem;
                       padding: 1rem 1.25rem 1.2rem; margin-bottom: 1.5rem;
                       box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  .sponsors-header {{ display: flex; align-items: center; justify-content: space-between;
                      flex-wrap: wrap; gap: .5rem; margin-bottom: .75rem; }}
  .sponsors-header h2 {{ margin: 0; }}
  .sponsor-search {{ padding: .35rem .65rem; border: 1px solid #cbd5e1;
                     border-radius: .35rem; font-size: .8rem; outline: none;
                     width: 200px; }}
  .sponsor-search:focus {{ border-color: #93c5fd; }}
  .chips-wrap {{ display: flex; flex-wrap: wrap; gap: .4rem; max-height: 180px;
                 overflow-y: auto; scrollbar-width: thin; padding-right: 2px; }}
  .chips-wrap::-webkit-scrollbar {{ width: 4px; }}
  .chips-wrap::-webkit-scrollbar-thumb {{ background: #cbd5e1; border-radius: 9999px; }}
  .chip {{ display: inline-flex; align-items: center; gap: .3rem; cursor: pointer;
           background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 9999px;
           padding: .22rem .65rem; font-size: .78rem; font-weight: 500;
           transition: all .12s; user-select: none; white-space: nowrap; }}
  .chip:hover {{ background: #dbeafe; border-color: #93c5fd; color: #1d4ed8; }}
  .chip.active {{ background: #2563eb; border-color: #2563eb; color: #fff; }}
  .chip .chip-count {{ font-size: .68rem; opacity: .75; }}

  /* Recently posted */
  .recent-section {{ background: #fff; border: 1px solid #e2e8f0; border-radius: .75rem;
                     padding: 1rem 1.25rem 1.2rem; margin-bottom: 1.75rem;
                     box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  .cards-scroll {{ display: flex; gap: .8rem; overflow-x: auto; padding-bottom: .4rem;
                   scrollbar-width: thin; }}
  .cards-scroll::-webkit-scrollbar {{ height: 5px; }}
  .cards-scroll::-webkit-scrollbar-thumb {{ background: #cbd5e1; border-radius: 9999px; }}
  .card {{ flex: 0 0 255px; border: 1px solid #e2e8f0; border-radius: .6rem;
           padding: .85rem .95rem; background: #f8fafc; display: flex;
           flex-direction: column; gap: .4rem; transition: box-shadow .15s; }}
  .card:hover {{ box-shadow: 0 4px 14px rgba(0,0,0,.1); background: #fff; }}
  .card-header {{ display: flex; align-items: flex-start; gap: .35rem; flex-wrap: wrap; }}
  .card-company {{ font-weight: 700; font-size: .9rem; color: #0f172a; }}
  .card-role {{ font-size: .8rem; color: #334155; line-height: 1.35; flex: 1; }}
  .card-meta {{ display: flex; flex-wrap: wrap; gap: .25rem .55rem;
               font-size: .73rem; color: #64748b; }}
  .new-badge {{ display: inline-block; background: #16a34a; color: #fff;
                font-size: .62rem; font-weight: 700; padding: .1rem .32rem;
                border-radius: .22rem; letter-spacing: .03em; white-space: nowrap; }}
  .source-tag {{ display: inline-block; color: #fff; padding: .12rem .45rem;
                 border-radius: .22rem; font-size: .68rem; font-weight: 600;
                 white-space: nowrap; }}
  .apply-btn {{ display: inline-block; background: #2563eb; color: #fff;
                padding: .28rem .7rem; border-radius: .35rem; text-decoration: none;
                font-size: .78rem; font-weight: 600; margin-top: auto; }}
  .apply-btn:hover {{ background: #1d4ed8; }}

  /* Filter bar */
  .filter-bar {{ display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: .85rem; }}
  .filter-bar input, .filter-bar select {{
    padding: .42rem .7rem; border: 1px solid #cbd5e1; border-radius: .35rem;
    font-size: .85rem; outline: none; }}
  .filter-bar input {{ flex: 1; min-width: 200px; }}
  .filter-bar input:focus {{ border-color: #93c5fd; }}

  /* Table */
  .table-wrap {{ overflow-x: auto; border-radius: .5rem;
                 box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
           min-width: 700px; }}
  th {{ background: #1e293b; color: #f8fafc; text-align: left;
        padding: .6rem .9rem; font-size: .75rem; text-transform: uppercase;
        letter-spacing: .05em; cursor: pointer; user-select: none;
        white-space: nowrap; }}
  th:hover {{ background: #334155; }}
  td {{ padding: .6rem .9rem; border-bottom: 1px solid #f1f5f9;
        font-size: .84rem; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #f8fafc; }}
  .score-badge {{ display: inline-block; color: #fff; padding: .18rem .45rem;
                  border-radius: 9999px; font-size: .72rem; font-weight: 700; }}
  .no-link {{ color: #94a3b8; }}

  /* Loading overlay */
  #loadingOverlay {{ position: fixed; inset: 0; background: rgba(248,250,252,.85);
                     display: flex; flex-direction: column; align-items: center;
                     justify-content: center; gap: 1rem; z-index: 100; }}
  .big-spinner {{ width: 44px; height: 44px; border: 4px solid #e2e8f0;
                  border-top-color: #2563eb; border-radius: 50%;
                  animation: spin .8s linear infinite; }}
  #loadingOverlay p {{ color: #64748b; font-size: .9rem; }}

  .hidden {{ display: none !important; }}
  footer {{ margin-top: 1.5rem; font-size: .73rem; color: #94a3b8; text-align: center; }}
</style>
</head>
<body>

<div id="loadingOverlay">
  <div class="big-spinner"></div>
  <p>Loading job data… this takes ~30 seconds on first run.</p>
</div>

<div class="topbar">
  <h1>🗂️ H1B Sponsor Job Matcher</h1>
  <button class="reload-btn" id="reloadBtn" onclick="triggerReload()">
    <span id="reloadIcon">↻</span> Reload Now
  </button>
</div>
<p class="meta" id="metaLine">Sources: {_SOURCE_LEGEND}</p>
<p class="next-refresh" id="nextRefresh"></p>

<div class="status-bar">
  <span class="dot" id="statusDot"></span>
  <span id="statusText">Connecting…</span>
</div>

<div class="stats" id="statsBar" style="display:none">
  <div class="stat"><div class="stat-num" id="statJobs">—</div><div class="stat-lbl">Matched Jobs</div></div>
  <div class="stat"><div class="stat-num" id="statCompanies">—</div><div class="stat-lbl">Companies</div></div>
  <div class="stat"><div class="stat-num" id="statRecent">—</div><div class="stat-lbl">Recently Posted</div></div>
  <div class="stat"><div class="stat-num">{MATCH_THRESHOLD}</div><div class="stat-lbl">Min Match Score</div></div>
</div>

<div class="sponsors-section" id="sponsorsSection" style="display:none">
  <div class="sponsors-header">
    <h2>🏢 H1B Sponsoring Companies <span style="font-size:.8rem;font-weight:400;color:#64748b" id="sponsorSubtitle"></span></h2>
    <input class="sponsor-search" id="sponsorSearch" placeholder="Filter companies…" oninput="filterChips()">
  </div>
  <div class="chips-wrap" id="chipsWrap"></div>
</div>

<div class="recent-section" id="recentSection" style="display:none">
  <h2>🔥 Recently Posted <span style="font-size:.8rem;font-weight:400;color:#64748b">(newest first · with apply links)</span></h2>
  <div class="cards-scroll" id="cardsScroll"></div>
</div>

<div class="filter-bar">
  <input type="text" id="search" placeholder="Search company, role, or location…" oninput="filterTable()">
  <select id="srcFilter" onchange="filterTable()">
    <option value="">All Sources</option>
    {_SOURCE_OPTIONS}
  </select>
</div>

<div class="table-wrap">
  <table id="resultsTable">
    <thead>
      <tr>
        <th onclick="sortTable(0)">Company ↕</th>
        <th onclick="sortTable(1)">Role ↕</th>
        <th onclick="sortTable(2)">Location ↕</th>
        <th onclick="sortTable(3)">Posted ↕</th>
        <th onclick="sortTable(4)">Source</th>
        <th onclick="sortTable(5)">Score ↕</th>
        <th>Apply</th>
      </tr>
    </thead>
    <tbody id="tableBody"></tbody>
  </table>
</div>

<footer>
  H1B data: USCIS Employer Information CSV &nbsp;|&nbsp; Jobs: GitHub (updated hourly)
</footer>

<script>
const SRC_COLORS = {_SRC_COLORS_JSON};
let allJobs = [];
let sortDir = {{}};
let pollTimer = null;

// ── Utilities ──────────────────────────────────────────────────────────────
function fmtTime(iso) {{
  if (!iso) return '—';
  return new Date(iso).toLocaleTimeString([], {{hour:'2-digit', minute:'2-digit'}});
}}
function fmtDateTime(iso) {{
  if (!iso) return '—';
  return new Date(iso).toLocaleString();
}}
function isNew(isoStr) {{
  if (!isoStr) return false;
  return (Date.now() - new Date(isoStr).getTime()) < 7 * 86400000;
}}

// ── Render ─────────────────────────────────────────────────────────────────
let activeChip = null;

function renderAll(jobs) {{
  allJobs = jobs;

  // Stats
  const companies = [...new Set(jobs.map(j => j.company))].sort();
  const recent = jobs
    .filter(j => j.apply_url && j.date_sort_iso && isNew(j.date_sort_iso))
    .sort((a, b) => b.date_sort_iso.localeCompare(a.date_sort_iso))
    .slice(0, 20);

  document.getElementById('statJobs').textContent = jobs.length.toLocaleString();
  document.getElementById('statCompanies').textContent = companies.length.toLocaleString();
  document.getElementById('statRecent').textContent = recent.length;
  document.getElementById('statsBar').style.display = '';

  // Sponsoring companies chips
  const countMap = {{}};
  jobs.forEach(j => {{ countMap[j.company] = (countMap[j.company] || 0) + 1; }});
  const chipsWrap = document.getElementById('chipsWrap');
  chipsWrap.innerHTML = companies.map(c =>
    `<span class="chip" data-company="${{c}}" onclick="toggleChip(this, '${{c.replace(/'/g, "\\'")}}')">${{c}} <span class="chip-count">${{countMap[c]}}</span></span>`
  ).join('');
  document.getElementById('sponsorSubtitle').textContent = `(${{companies.length}} companies with open roles)`;
  document.getElementById('sponsorsSection').style.display = '';

  // Recently posted cards
  const scroll = document.getElementById('cardsScroll');
  scroll.innerHTML = recent.length
    ? recent.map(j => cardHTML(j)).join('')
    : '<p style="color:#94a3b8;font-size:.85rem">No jobs posted in the last 7 days.</p>';
  document.getElementById('recentSection').style.display = '';

  // Table
  renderTable(jobs);

  document.getElementById('loadingOverlay').classList.add('hidden');
}}

function cardHTML(j) {{
  const srcColor = SRC_COLORS[j.source] || '#6b7280';
  const srcShort = j.source.split('/')[0];
  const newBadge = isNew(j.date_sort_iso) ? '<span class="new-badge">NEW</span>' : '';
  return `
  <div class="card">
    <div class="card-header">
      <span class="card-company">${{j.company}}</span>${{newBadge}}
    </div>
    <div class="card-role">${{j.role}}</div>
    <div class="card-meta">
      <span>📍 ${{j.location}}</span>
      <span>📅 ${{j.date_label || '—'}}</span>
      <span class="source-tag" style="background:${{srcColor}}">${{srcShort}}</span>
    </div>
    <a href="${{j.apply_url}}" target="_blank" class="apply-btn">Apply ↗</a>
  </div>`;
}}

function rowHTML(j) {{
  const srcColor = SRC_COLORS[j.source] || '#6b7280';
  const score = j.match_score || 0;
  const badgeColor = score >= 95 ? '#16a34a' : score >= 88 ? '#ca8a04' : '#dc2626';
  const newBadge = isNew(j.date_sort_iso) ? ' <span class="new-badge">NEW</span>' : '';
  const applyBtn = j.apply_url
    ? `<a href="${{j.apply_url}}" target="_blank" class="apply-btn">Apply ↗</a>`
    : '<span class="no-link">—</span>';
  return `<tr>
    <td><strong>${{j.company}}</strong><br><small style="color:#6b7280">H1B: ${{j.h1b_match}}</small></td>
    <td>${{j.role}}</td>
    <td>${{j.location}}</td>
    <td>${{j.date_label || '—'}}${{newBadge}}</td>
    <td><span class="source-tag" style="background:${{srcColor}}">${{j.source}}</span></td>
    <td><span class="score-badge" style="background:${{badgeColor}}">${{score.toFixed(0)}}</span></td>
    <td>${{applyBtn}}</td>
  </tr>`;
}}

function renderTable(jobs) {{
  document.getElementById('tableBody').innerHTML = jobs.map(rowHTML).join('');
}}

// ── Chip toggle ────────────────────────────────────────────────────────────
function toggleChip(el, company) {{
  if (activeChip === company) {{
    // Deselect — clear filter
    activeChip = null;
    el.classList.remove('active');
    document.getElementById('search').value = '';
  }} else {{
    // Deselect previous
    document.querySelectorAll('.chip.active').forEach(c => c.classList.remove('active'));
    activeChip = company;
    el.classList.add('active');
    document.getElementById('search').value = company;
  }}
  filterTable();
  if (activeChip) {{
    document.getElementById('resultsTable').scrollIntoView({{behavior:'smooth', block:'start'}});
  }}
}}

function filterChips() {{
  const q = document.getElementById('sponsorSearch').value.toLowerCase();
  document.querySelectorAll('.chip').forEach(chip => {{
    chip.style.display = chip.dataset.company.toLowerCase().includes(q) ? '' : 'none';
  }});
}}

// ── Filter ─────────────────────────────────────────────────────────────────
function filterTable() {{
  const q   = document.getElementById('search').value.toLowerCase();
  const src = document.getElementById('srcFilter').value.toLowerCase();
  // If user cleared the search box manually, deselect any active chip
  if (!q && activeChip) {{
    activeChip = null;
    document.querySelectorAll('.chip.active').forEach(c => c.classList.remove('active'));
  }}
  document.querySelectorAll('#tableBody tr').forEach(row => {{
    const text = row.textContent.toLowerCase();
    row.classList.toggle('hidden', !(text.includes(q) && (!src || text.includes(src))));
  }});
}}

// ── Sort ───────────────────────────────────────────────────────────────────
function sortTable(col) {{
  const tbody = document.getElementById('tableBody');
  const rows  = [...tbody.querySelectorAll('tr')];
  const dir   = (sortDir[col] = !sortDir[col]) ? 1 : -1;
  rows.sort((a, b) => {{
    const ta = a.cells[col].textContent.trim();
    const tb = b.cells[col].textContent.trim();
    const na = parseFloat(ta), nb = parseFloat(tb);
    return (!isNaN(na) && !isNaN(nb)) ? dir*(na-nb) : dir*ta.localeCompare(tb);
  }});
  rows.forEach(r => tbody.appendChild(r));
}}

// ── Status polling ─────────────────────────────────────────────────────────
async function pollStatus() {{
  try {{
    const s = await fetch('/api/status').then(r => r.json());
    const dot  = document.getElementById('statusDot');
    const text = document.getElementById('statusText');
    const btn  = document.getElementById('reloadBtn');
    const icon = document.getElementById('reloadIcon');
    const next = document.getElementById('nextRefresh');

    if (s.is_running) {{
      dot.className = 'dot spin';
      text.textContent = 'Fetching live job data…';
      btn.disabled = true;
      icon.outerHTML = '<span class="spinner" id="reloadIcon"></span>';
    }} else if (s.error) {{
      dot.className = 'dot error';
      text.textContent = 'Error: ' + s.error;
      btn.disabled = false;
      document.getElementById('reloadIcon').outerHTML = '<span id="reloadIcon">↻</span>';
    }} else {{
      dot.className = 'dot live';
      text.textContent = `Last updated ${{fmtDateTime(s.last_updated)}} · ${{s.job_count}} jobs`;
      btn.disabled = false;
      const el = document.getElementById('reloadIcon');
      if (el) el.outerHTML = '<span id="reloadIcon">↻</span>';
      next.textContent = s.next_refresh ? `⏱ Next auto-refresh at ${{fmtTime(s.next_refresh)}}` : '';

      // Load jobs if cache populated and we have nothing yet
      if (s.job_count > 0 && allJobs.length === 0) {{
        const jobs = await fetch('/api/jobs').then(r => r.json());
        renderAll(jobs);
      }}
    }}
  }} catch(e) {{
    console.error('Poll error:', e);
  }}
  pollTimer = setTimeout(pollStatus, 3000);
}}

// ── Reload ─────────────────────────────────────────────────────────────────
async function triggerReload() {{
  try {{
    const res = await fetch('/api/reload', {{method:'POST'}});
    if (res.status === 409) {{ alert('Already running, please wait.'); return; }}
    // After reload starts, wait then re-fetch jobs
    clearTimeout(pollTimer);
    await pollStatus();
    await new Promise(r => setTimeout(r, 2000));
    // Poll until done, then reload jobs
    const wait = setInterval(async () => {{
      const s = await fetch('/api/status').then(r => r.json());
      if (!s.is_running && s.last_updated) {{
        clearInterval(wait);
        allJobs = [];   // force re-render
        const jobs = await fetch('/api/jobs').then(r => r.json());
        renderAll(jobs);
        pollStatus();
      }}
    }}, 3000);
  }} catch(e) {{ console.error(e); }}
}}

// ── Boot ───────────────────────────────────────────────────────────────────
pollStatus();
</script>
</body>
</html>"""


@app.get("/")
def index():
    return _HTML


if __name__ == "__main__":
    print("Starting H1B Job Matcher web server on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
