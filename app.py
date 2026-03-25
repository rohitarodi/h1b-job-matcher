"""
H1B Job Matcher — Web Server
----------------------------
Flask app that serves a live dashboard + job application tracker.
• Runs the matcher on startup and every hour in a background thread.
• POST /api/reload triggers an immediate refresh.
• GET  /api/status  returns cache state (is_running, last_updated, counts).
• GET  /api/jobs    returns the full matched-jobs JSON array.
• POST /api/applications  upsert an application (from gmail_tracker.py)
• GET  /api/applications  returns all tracked applications
• GET  /api/tracker/tree  returns tree data for D3 visualization
"""

import threading
import time
import sqlite3
import os
import sys
from datetime import datetime

from flask import Flask, jsonify, request

from h1b_matcher import run_matcher, GITHUB_SOURCES, MATCH_THRESHOLD

app = Flask(__name__)

# ── SQLite setup ──────────────────────────────────────────────────────────────
DB_PATH = os.environ.get('TRACKER_DB', '/data/tracker.db')
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                company       TEXT NOT NULL,
                role          TEXT,
                status        TEXT DEFAULT 'applied',
                applied_date  TEXT,
                last_updated  TEXT,
                source        TEXT DEFAULT 'linkedin',
                email_subject TEXT,
                UNIQUE(company, role)
            )
        """)
        conn.commit()
    print(f"[DB] Initialized at {DB_PATH}", flush=True)

init_db()

STATUS_ORDER = ['applied', 'screening', 'interview', 'offer', 'rejected']

# ── Shared cache ──────────────────────────────────────────────────────────────
_cache = {
    "jobs":         [],
    "last_updated": None,
    "is_running":   False,
    "error":        None,
    "next_refresh":  None,
}
_lock = threading.Lock()
REFRESH_INTERVAL = 3600


# ── Background worker ─────────────────────────────────────────────────────────
def _do_run():
    with _lock:
        if _cache["is_running"]:
            return
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
    while True:
        _do_run()
        time.sleep(REFRESH_INTERVAL)


threading.Thread(target=_scheduler, daemon=True).start()


# ── Existing API routes ───────────────────────────────────────────────────────
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


# ── Tracker API ───────────────────────────────────────────────────────────────
@app.post("/api/applications")
def upsert_application():
    data = request.get_json(force=True)
    company = (data.get('company') or '').strip()
    role    = (data.get('role') or '').strip() or None
    status  = (data.get('status') or 'applied').lower()
    source  = data.get('source', 'gmail')
    subject = data.get('email_subject', '')
    applied = data.get('applied_date', '')
    now     = datetime.now().isoformat()

    if not company or status not in STATUS_ORDER:
        return jsonify({'error': 'invalid data'}), 400

    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM applications WHERE company=? AND (role=? OR (role IS NULL AND ?=''))",
            (company, role or '', role or '')
        ).fetchone()

        is_new = existing is None
        status_changed = False

        if is_new:
            conn.execute("""
                INSERT INTO applications (company, role, status, applied_date, last_updated, source, email_subject)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (company, role, status, applied, now, source, subject))
        else:
            old_status = existing['status']
            # Only upgrade status (applied→screening→interview→offer), allow rejected at any point
            old_idx = STATUS_ORDER.index(old_status) if old_status in STATUS_ORDER else 0
            new_idx = STATUS_ORDER.index(status) if status in STATUS_ORDER else 0

            if status == 'rejected' or new_idx > old_idx:
                status_changed = old_status != status
                conn.execute("""
                    UPDATE applications
                    SET status=?, last_updated=?, email_subject=?
                    WHERE company=? AND (role=? OR (role IS NULL AND ?=''))
                """, (status, now, subject, company, role or '', role or ''))
            else:
                status_changed = False

        conn.commit()

    return jsonify({
        'company': company,
        'role': role,
        'status': status,
        'is_new': is_new,
        'status_changed': status_changed
    })


@app.get("/api/applications")
def get_applications():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM applications ORDER BY last_updated DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/tracker/tree")
def tracker_tree():
    """Return hierarchical tree data for D3 visualization."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM applications").fetchall()

    apps = [dict(r) for r in rows]
    total = len(apps)

    # Group by status
    by_status = {s: [] for s in STATUS_ORDER}
    for a in apps:
        s = a.get('status', 'applied')
        if s in by_status:
            by_status[s].append(a)

    # Build tree: root → applied → screening → interview → offer/rejected
    def companies_list(status):
        return [{'name': a['company'], 'role': a['role'] or '', 'status': status}
                for a in by_status[status]]

    tree = {
        'name': f'All Applications ({total})',
        'count': total,
        'status': 'root',
        'children': [
            {
                'name': f"Applied ({len(apps)})",
                'count': len(apps),
                'status': 'applied',
                'companies': companies_list('applied'),
                'children': [
                    {
                        'name': f"Screening ({len(by_status['screening'])})",
                        'count': len(by_status['screening']),
                        'status': 'screening',
                        'companies': companies_list('screening'),
                        'children': [
                            {
                                'name': f"Interview ({len(by_status['interview'])})",
                                'count': len(by_status['interview']),
                                'status': 'interview',
                                'companies': companies_list('interview'),
                                'children': [
                                    {
                                        'name': f"Offer 🎉 ({len(by_status['offer'])})",
                                        'count': len(by_status['offer']),
                                        'status': 'offer',
                                        'companies': companies_list('offer'),
                                        'children': []
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'name': f"Rejected ✗ ({len(by_status['rejected'])})",
                        'count': len(by_status['rejected']),
                        'status': 'rejected',
                        'companies': companies_list('rejected'),
                        'children': []
                    }
                ]
            }
        ]
    }

    stats = {s: len(by_status[s]) for s in STATUS_ORDER}
    stats['total'] = total
    stats['no_response'] = len([a for a in apps if a['status'] == 'applied'])

    return jsonify({'tree': tree, 'stats': stats})


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
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, sans-serif; background: #f8fafc; color: #1e293b;
         margin: 0; padding: 1.25rem; }}
  a {{ color: inherit; }}
  h1 {{ font-size: 1.6rem; margin: 0 0 .2rem; }}
  h2 {{ font-size: 1.1rem; font-weight: 700; margin: 0 0 .75rem; }}

  /* Tabs */
  .tabs {{ display: flex; gap: .25rem; margin-bottom: 1.5rem; border-bottom: 2px solid #e2e8f0; }}
  .tab {{ padding: .6rem 1.2rem; cursor: pointer; font-weight: 600; font-size: .9rem;
          color: #64748b; border-bottom: 2px solid transparent; margin-bottom: -2px;
          transition: all .15s; }}
  .tab:hover {{ color: #1e293b; }}
  .tab.active {{ color: #2563eb; border-bottom-color: #2563eb; }}
  .tab-content {{ display: none; }}
  .tab-content.active {{ display: block; }}

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

  /* ── Tracker Tab ── */
  .tracker-stats {{ display: flex; gap: .85rem; flex-wrap: wrap; margin-bottom: 1.5rem; }}
  .tracker-stat {{ background: #fff; border: 1px solid #e2e8f0; border-radius: .5rem;
                   padding: .65rem 1.1rem; cursor: pointer; transition: all .15s; }}
  .tracker-stat:hover {{ border-color: #93c5fd; box-shadow: 0 2px 8px rgba(0,0,0,.08); }}
  .tracker-stat .stat-num {{ font-size: 1.4rem; font-weight: 700; }}
  .tracker-stat .stat-lbl {{ font-size: .7rem; color: #64748b; text-transform: uppercase;
                              letter-spacing: .05em; }}
  .tracker-stat.applied   .stat-num {{ color: #2563eb; }}
  .tracker-stat.screening .stat-num {{ color: #f59e0b; }}
  .tracker-stat.interview .stat-num {{ color: #8b5cf6; }}
  .tracker-stat.offer     .stat-num {{ color: #16a34a; }}
  .tracker-stat.rejected  .stat-num {{ color: #dc2626; }}

  /* D3 Tree */
  #treeContainer {{
    background: #fff; border: 1px solid #e2e8f0; border-radius: .75rem;
    padding: 1rem; margin-bottom: 1.5rem; overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,.06);
    min-height: 500px;
  }}
  #treeContainer svg {{ width: 100%; }}

  .node circle {{
    stroke-width: 2.5px;
    cursor: pointer;
    transition: r .2s;
  }}
  .node circle:hover {{ r: 12; }}
  .node text {{
    font-size: 13px;
    font-family: system-ui, sans-serif;
  }}
  .link {{
    fill: none;
    stroke: #cbd5e1;
    stroke-width: 1.5px;
  }}

  /* Company tooltip / panel */
  #companyPanel {{
    position: fixed; right: 1rem; top: 5rem;
    background: #fff; border: 1px solid #e2e8f0;
    border-radius: .75rem; padding: 1rem 1.25rem;
    max-width: 320px; max-height: 70vh; overflow-y: auto;
    box-shadow: 0 8px 24px rgba(0,0,0,.12);
    display: none; z-index: 50;
  }}
  #companyPanel h3 {{ margin: 0 0 .5rem; font-size: 1rem; }}
  #companyPanel .close-btn {{
    position: absolute; top: .6rem; right: .8rem;
    cursor: pointer; font-size: 1.1rem; color: #94a3b8;
  }}
  #companyPanel .close-btn:hover {{ color: #1e293b; }}
  .company-item {{
    padding: .35rem 0; border-bottom: 1px solid #f1f5f9;
    font-size: .84rem;
  }}
  .company-item:last-child {{ border-bottom: none; }}
  .company-item .role {{ font-size: .75rem; color: #64748b; }}

  /* Tracker table */
  .tracker-filter {{ display: flex; gap: .5rem; margin-bottom: .75rem; flex-wrap: wrap; }}
  .tracker-filter input, .tracker-filter select {{
    padding: .4rem .7rem; border: 1px solid #cbd5e1; border-radius: .35rem;
    font-size: .84rem; outline: none; }}
  .tracker-filter input {{ flex: 1; min-width: 180px; }}
  .status-badge {{
    display: inline-block; padding: .15rem .5rem; border-radius: 9999px;
    font-size: .72rem; font-weight: 600; color: #fff;
  }}
  .status-applied   {{ background: #2563eb; }}
  .status-screening {{ background: #f59e0b; }}
  .status-interview {{ background: #8b5cf6; }}
  .status-offer     {{ background: #16a34a; }}
  .status-rejected  {{ background: #dc2626; }}

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

<!-- Tabs -->
<div class="tabs">
  <div class="tab active" onclick="switchTab('jobs')">💼 Job Listings</div>
  <div class="tab" onclick="switchTab('tracker')">📊 Application Tracker</div>
</div>

<!-- ═══════════════ JOB LISTINGS TAB ═══════════════ -->
<div id="tab-jobs" class="tab-content active">

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

</div><!-- /tab-jobs -->

<!-- ═══════════════ TRACKER TAB ═══════════════ -->
<div id="tab-tracker" class="tab-content">

  <div class="tracker-stats" id="trackerStats">
    <div class="tracker-stat applied">
      <div class="stat-num" id="tTotal">—</div>
      <div class="stat-lbl">Total Applied</div>
    </div>
    <div class="tracker-stat screening">
      <div class="stat-num" id="tScreening">—</div>
      <div class="stat-lbl">Screening</div>
    </div>
    <div class="tracker-stat interview">
      <div class="stat-num" id="tInterview">—</div>
      <div class="stat-lbl">Interviews</div>
    </div>
    <div class="tracker-stat offer">
      <div class="stat-num" id="tOffer">—</div>
      <div class="stat-lbl">Offers 🎉</div>
    </div>
    <div class="tracker-stat rejected">
      <div class="stat-num" id="tRejected">—</div>
      <div class="stat-lbl">Rejected</div>
    </div>
  </div>

  <div style="background:#fff;border:1px solid #e2e8f0;border-radius:.75rem;padding:1rem 1.25rem;margin-bottom:1.5rem;box-shadow:0 1px 3px rgba(0,0,0,.06)">
    <h2 style="margin-bottom:1rem">🌳 Application Pipeline Tree</h2>
    <p style="font-size:.8rem;color:#64748b;margin:-0.5rem 0 1rem">Click any node to see companies at that stage</p>
    <div id="treeContainer"></div>
  </div>

  <!-- Company panel (shown on node click) -->
  <div id="companyPanel">
    <span class="close-btn" onclick="document.getElementById('companyPanel').style.display='none'">✕</span>
    <h3 id="panelTitle"></h3>
    <div id="panelList"></div>
  </div>

  <div style="background:#fff;border:1px solid #e2e8f0;border-radius:.75rem;padding:1rem 1.25rem;box-shadow:0 1px 3px rgba(0,0,0,.06)">
    <h2 style="margin-bottom:.75rem">📋 All Applications</h2>
    <div class="tracker-filter">
      <input type="text" id="trackerSearch" placeholder="Search company or role…" oninput="filterTrackerTable()">
      <select id="trackerStatusFilter" onchange="filterTrackerTable()">
        <option value="">All Statuses</option>
        <option value="applied">Applied</option>
        <option value="screening">Screening</option>
        <option value="interview">Interview</option>
        <option value="offer">Offer</option>
        <option value="rejected">Rejected</option>
      </select>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Company</th>
            <th>Role</th>
            <th>Status</th>
            <th>Last Updated</th>
          </tr>
        </thead>
        <tbody id="trackerTableBody"></tbody>
      </table>
    </div>
  </div>

</div><!-- /tab-tracker -->

<footer>
  H1B data: USCIS Employer Information CSV &nbsp;|&nbsp; Jobs: GitHub (updated hourly) &nbsp;|&nbsp; Tracker: Gmail auto-sync
</footer>

<script>
const SRC_COLORS = {_SRC_COLORS_JSON};
let allJobs = [];
let sortDir = {{}};
let pollTimer = null;
let allApps = [];

// ── Tab switching ──────────────────────────────────────────────────────────
function switchTab(name) {{
  document.querySelectorAll('.tab').forEach((t,i) => {{
    const names = ['jobs','tracker'];
    t.classList.toggle('active', names[i] === name);
  }});
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (name === 'tracker') loadTracker();
}}

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

// ── Render jobs ─────────────────────────────────────────────────────────────
let activeChip = null;

function renderAll(jobs) {{
  allJobs = jobs;
  const companies = [...new Set(jobs.map(j => j.company))].sort();
  const recent = jobs
    .filter(j => j.apply_url && j.date_sort_iso && isNew(j.date_sort_iso))
    .sort((a, b) => b.date_sort_iso.localeCompare(a.date_sort_iso))
    .slice(0, 20);

  document.getElementById('statJobs').textContent = jobs.length.toLocaleString();
  document.getElementById('statCompanies').textContent = companies.length.toLocaleString();
  document.getElementById('statRecent').textContent = recent.length;
  document.getElementById('statsBar').style.display = '';

  const countMap = {{}};
  jobs.forEach(j => {{ countMap[j.company] = (countMap[j.company] || 0) + 1; }});
  const chipsWrap = document.getElementById('chipsWrap');
  chipsWrap.innerHTML = companies.map(c =>
    `<span class="chip" data-company="${{c}}" onclick="toggleChip(this, '${{c.replace(/'/g, "\\'")}}')">${{c}} <span class="chip-count">${{countMap[c]}}</span></span>`
  ).join('');
  document.getElementById('sponsorSubtitle').textContent = `(${{companies.length}} companies with open roles)`;
  document.getElementById('sponsorsSection').style.display = '';

  const scroll = document.getElementById('cardsScroll');
  scroll.innerHTML = recent.length
    ? recent.map(j => cardHTML(j)).join('')
    : '<p style="color:#94a3b8;font-size:.85rem">No jobs posted in the last 7 days.</p>';
  document.getElementById('recentSection').style.display = '';

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
    activeChip = null;
    el.classList.remove('active');
    document.getElementById('search').value = '';
  }} else {{
    document.querySelectorAll('.chip.active').forEach(c => c.classList.remove('active'));
    activeChip = company;
    el.classList.add('active');
    document.getElementById('search').value = company;
  }}
  filterTable();
  if (activeChip) document.getElementById('resultsTable').scrollIntoView({{behavior:'smooth', block:'start'}});
}}

function filterChips() {{
  const q = document.getElementById('sponsorSearch').value.toLowerCase();
  document.querySelectorAll('.chip').forEach(chip => {{
    chip.style.display = chip.dataset.company.toLowerCase().includes(q) ? '' : 'none';
  }});
}}

function filterTable() {{
  const q   = document.getElementById('search').value.toLowerCase();
  const src = document.getElementById('srcFilter').value.toLowerCase();
  if (!q && activeChip) {{
    activeChip = null;
    document.querySelectorAll('.chip.active').forEach(c => c.classList.remove('active'));
  }}
  document.querySelectorAll('#tableBody tr').forEach(row => {{
    const text = row.textContent.toLowerCase();
    row.classList.toggle('hidden', !(text.includes(q) && (!src || text.includes(src))));
  }});
}}

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

// ── Tracker ────────────────────────────────────────────────────────────────
async function loadTracker() {{
  try {{
    const [treeRes, appsRes] = await Promise.all([
      fetch('/api/tracker/tree').then(r => r.json()),
      fetch('/api/applications').then(r => r.json())
    ]);

    const stats = treeRes.stats;
    document.getElementById('tTotal').textContent     = stats.total;
    document.getElementById('tScreening').textContent = stats.screening;
    document.getElementById('tInterview').textContent = stats.interview;
    document.getElementById('tOffer').textContent     = stats.offer;
    document.getElementById('tRejected').textContent  = stats.rejected;

    renderTree(treeRes.tree);

    allApps = appsRes;
    renderTrackerTable(appsRes);
  }} catch(e) {{
    console.error('Tracker load error:', e);
  }}
}}

// ── D3 Tree ────────────────────────────────────────────────────────────────
const STATUS_COLORS = {{
  root:      '#1e293b',
  applied:   '#2563eb',
  screening: '#f59e0b',
  interview: '#8b5cf6',
  offer:     '#16a34a',
  rejected:  '#dc2626',
}};

function renderTree(data) {{
  const container = document.getElementById('treeContainer');
  container.innerHTML = '';

  const width  = container.clientWidth || 900;
  const height = 480;
  const margin = {{top: 40, right: 160, bottom: 20, left: 80}};

  const svg = d3.select('#treeContainer')
    .append('svg')
    .attr('viewBox', `0 0 ${{width}} ${{height}}`)
    .attr('preserveAspectRatio', 'xMidYMid meet');

  const g = svg.append('g')
    .attr('transform', `translate(${{margin.left}},${{margin.top}})`);

  const root = d3.hierarchy(data);
  const treeLayout = d3.tree()
    .size([height - margin.top - margin.bottom, width - margin.left - margin.right]);

  treeLayout(root);

  // Links
  g.selectAll('.link')
    .data(root.links())
    .join('path')
    .attr('class', 'link')
    .attr('d', d3.linkHorizontal()
      .x(d => d.y)
      .y(d => d.x));

  // Nodes
  const node = g.selectAll('.node')
    .data(root.descendants())
    .join('g')
    .attr('class', 'node')
    .attr('transform', d => `translate(${{d.y}},${{d.x}})`)
    .style('cursor', 'pointer')
    .on('click', (event, d) => showCompanies(d.data));

  node.append('circle')
    .attr('r', d => Math.max(8, Math.min(28, 8 + (d.data.count || 0) / 10)))
    .style('fill', d => STATUS_COLORS[d.data.status] || '#94a3b8')
    .style('stroke', d => STATUS_COLORS[d.data.status] || '#94a3b8')
    .style('stroke-opacity', 0.3)
    .style('fill-opacity', 0.85);

  node.append('text')
    .attr('dy', '.35em')
    .attr('x', d => d.children ? -14 : 14)
    .style('text-anchor', d => d.children ? 'end' : 'start')
    .style('font-weight', '600')
    .style('fill', '#1e293b')
    .text(d => d.data.name);
}}

function showCompanies(nodeData) {{
  const panel = document.getElementById('companyPanel');
  const companies = nodeData.companies || [];
  document.getElementById('panelTitle').textContent = nodeData.name;

  if (!companies.length) {{
    document.getElementById('panelList').innerHTML =
      '<p style="color:#94a3b8;font-size:.85rem">No companies at this stage yet.</p>';
  }} else {{
    document.getElementById('panelList').innerHTML = companies
      .map(c => `<div class="company-item">
        <strong>${{c.name}}</strong>
        ${{c.role ? `<div class="role">${{c.role}}</div>` : ''}}
      </div>`)
      .join('');
  }}
  panel.style.display = 'block';
}}

// ── Tracker table ──────────────────────────────────────────────────────────
function renderTrackerTable(apps) {{
  document.getElementById('trackerTableBody').innerHTML = apps.map(a => `
    <tr>
      <td><strong>${{a.company}}</strong></td>
      <td>${{a.role || '<span style="color:#94a3b8">—</span>'}}</td>
      <td><span class="status-badge status-${{a.status}}">${{a.status}}</span></td>
      <td style="font-size:.78rem;color:#64748b">${{fmtDateTime(a.last_updated)}}</td>
    </tr>
  `).join('');
}}

function filterTrackerTable() {{
  const q  = document.getElementById('trackerSearch').value.toLowerCase();
  const st = document.getElementById('trackerStatusFilter').value;
  const filtered = allApps.filter(a =>
    (!q  || (a.company+' '+(a.role||'')).toLowerCase().includes(q)) &&
    (!st || a.status === st)
  );
  renderTrackerTable(filtered);
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
    clearTimeout(pollTimer);
    await pollStatus();
    await new Promise(r => setTimeout(r, 2000));
    const wait = setInterval(async () => {{
      const s = await fetch('/api/status').then(r => r.json());
      if (!s.is_running && s.last_updated) {{
        clearInterval(wait);
        allJobs = [];
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
