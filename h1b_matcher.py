"""
H1B Job Matcher
---------------
Matches H1B-sponsoring companies from the USCIS employer CSV
against live new-grad job listings scraped from GitHub.

Sources:
  - speedyapply/2026-SWE-College-Jobs  (NEW_GRAD_USA.md)
  - vanshb03/New-Grad-2026             (README.md)

Output:
  - Console summary
  - h1b_matches.html  (full interactive report)
"""

import re
import sys
import warnings
import requests
import pandas as pd
from rapidfuzz import process, fuzz
from datetime import datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

# ── Config ────────────────────────────────────────────────────────────────────

CSV_PATH = Path(__file__).parent / "Employer Information.csv"

# In Docker the /output dir is mounted so the HTML can be retrieved from the host
_OUTPUT_DIR = Path("/output") if Path("/output").exists() else Path(__file__).parent

GITHUB_SOURCES = [
    {
        "name": "speedyapply/2026-SWE-College-Jobs",
        "url": "https://raw.githubusercontent.com/speedyapply/2026-SWE-College-Jobs/main/NEW_GRAD_USA.md",
        "repo_url": "https://github.com/speedyapply/2026-SWE-College-Jobs/blob/main/NEW_GRAD_USA.md",
    },
    {
        "name": "vanshb03/New-Grad-2026",
        "url": "https://raw.githubusercontent.com/vanshb03/New-Grad-2026/main/README.md",
        "repo_url": "https://github.com/vanshb03/New-Grad-2026",
    },
]

MATCH_THRESHOLD = 82   # fuzzy score 0-100; raise to be stricter, lower to catch more
OUTPUT_HTML = _OUTPUT_DIR / "h1b_matches.html"


# ── Step 1: Load H1B employer names ──────────────────────────────────────────

def load_h1b_employers(csv_path: Path) -> set[str]:
    """Return a set of normalised H1B employer names from the USCIS CSV."""
    print("📂  Loading H1B employer data …", flush=True)
    df = pd.read_csv(
        csv_path,
        encoding="utf-16",
        sep="\t",
        on_bad_lines="skip",
        dtype=str,
        low_memory=False,
    )

    # The employer column is the third column (index 2) — "Employer (Petitioner) Name"
    name_col = df.columns[2]
    employers = (
        df[name_col]
        .dropna()
        .str.strip()
        .str.upper()
        .unique()
        .tolist()
    )
    employers = [e for e in employers if e]          # drop empties
    print(f"   ✅  {len(employers):,} unique H1B employers loaded.\n", flush=True)
    return employers


# ── Step 2: Fetch & parse GitHub markdown tables ──────────────────────────────

# Regex patterns
_HTML_HREF   = re.compile(r'href=["\']([^"\']+)["\']')          # href="url"
_HTML_STRONG = re.compile(r'<strong>([^<]+)</strong>', re.I)    # <strong>text</strong>
_MD_LINK     = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')           # [text](url)
_HTML_TAGS   = re.compile(r'<[^>]+>')                           # any HTML tag


def _extract_url(cell: str) -> str:
    """Return the first HTTP URL found in a cell (HTML href or markdown link)."""
    # HTML <a href="url">
    m = _HTML_HREF.search(cell)
    if m and m.group(1).startswith("http"):
        return m.group(1)
    # Markdown [text](url)
    m = _MD_LINK.search(cell)
    if m and m.group(2).startswith("http"):
        return m.group(2)
    return ""


def _plain_text(cell: str) -> str:
    """Strip all HTML tags and markdown formatting, return plain text."""
    # Extract <strong> content first
    cell = _HTML_STRONG.sub(r"\1", cell)
    # Strip remaining HTML
    cell = _HTML_TAGS.sub("", cell)
    # Strip markdown bold/italic
    cell = re.sub(r"\*\*(.+?)\*\*", r"\1", cell)
    cell = re.sub(r"_(.+?)_", r"\1", cell)
    # Strip markdown links — keep just the text
    cell = _MD_LINK.sub(r"\1", cell)
    return cell.strip()


def _is_separator(cells: list[str]) -> bool:
    return all(re.fullmatch(r"[-: ]+", c) for c in cells if c.strip())


def _parse_date(date_str: str) -> tuple[str, datetime]:
    """
    Parse date strings from both sources into (display_label, sortable_datetime).
    vanshb03 format : "Mar 19", "Feb 15"
    speedyapply format: "2d", "1w", "3m"  (age relative to today)
    """
    s = date_str.strip()
    if not s or s == "—":
        return "—", datetime.min

    now = datetime.now()

    # Relative age: "2d", "1w", "3m"
    m = re.fullmatch(r"(\d+)([dwm])", s, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = timedelta(days=n) if unit == "d" else timedelta(weeks=n) if unit == "w" else timedelta(days=n * 30)
        dt = now - delta
        return dt.strftime("%b %d"), dt

    # Month-day: "Mar 19", "Feb 15"
    for fmt in ("%b %d", "%B %d", "%b. %d"):
        try:
            parsed = datetime.strptime(s, fmt).replace(year=now.year)
            # If parsed date is in the future, it must be last year
            if parsed > now + timedelta(days=1):
                parsed = parsed.replace(year=now.year - 1)
            return s, parsed
        except ValueError:
            continue

    return s, datetime.min


def parse_markdown_jobs(markdown: str, source_name: str) -> list[dict]:
    """
    Parse job table rows from mixed HTML/markdown GitHub pages.
    Handles multi-line rows caused by <details> tags.
    Returns list of dicts: company, role, location, apply_url, source.
    """
    jobs: list[dict] = []

    # ── Pre-process: join multi-line table rows ────────────────────────────
    # A row continuation is a non-empty line that doesn't start with '|'
    # but belongs to an ongoing <details> block inside a cell.
    merged_lines: list[str] = []
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if line.startswith("|"):
            merged_lines.append(line)
        elif merged_lines and merged_lines[-1].startswith("|"):
            # Continuation of a multi-line cell — append to previous row
            merged_lines[-1] += " " + line.strip()
        else:
            merged_lines.append(line)

    # ── Parse table rows ──────────────────────────────────────────────────
    header_cols: list[str] = []
    in_table = False

    for line in merged_lines:
        if not (line.startswith("|") and line.endswith("|")):
            if in_table:
                in_table = False
                header_cols = []
            continue

        # Split on literal '|', skip first/last empty strings
        raw_cells = line[1:-1].split("|")
        plain_cells = [_plain_text(c) for c in raw_cells]

        # Skip separator rows
        if _is_separator(plain_cells):
            continue

        lower_cells = [c.lower().strip() for c in plain_cells]

        # Header row detection
        if any(k in lower_cells for k in ("company", "employer")):
            header_cols = lower_cells
            in_table = True
            continue

        if not in_table or not header_cols:
            continue

        # Map header → raw cell content (keep HTML for URL extraction)
        row_raw: dict[str, str] = {}
        row_txt: dict[str, str] = {}
        for i, col in enumerate(header_cols):
            if i < len(raw_cells):
                row_raw[col] = raw_cells[i]
                row_txt[col] = plain_cells[i]

        # ── Company ───────────────────────────────────────────────────────
        company = ""
        for key in ("company", "employer", "company name"):
            if key in row_raw:
                # Prefer <strong> text, fallback to plain
                m = _HTML_STRONG.search(row_raw[key])
                company = m.group(1).strip() if m else row_txt.get(key, "")
                break
        if not company:
            continue

        # ── Role ──────────────────────────────────────────────────────────
        role = ""
        for key in ("position", "role", "title", "job title"):
            if key in row_txt:
                role = row_txt[key]
                break

        # ── Location ─────────────────────────────────────────────────────
        location = ""
        for key in ("location", "locations", "city"):
            if key in row_txt:
                location = row_txt[key]
                break

        # ── Apply URL ─────────────────────────────────────────────────────
        apply_url = ""
        # Check dedicated link columns first
        for key in ("posting", "apply", "application/link", "link", "url", "application"):
            if key in row_raw:
                apply_url = _extract_url(row_raw[key])
                if apply_url:
                    break
        # Fallback: scan all cells for any HTTP URL
        if not apply_url:
            for cell_raw in row_raw.values():
                apply_url = _extract_url(cell_raw)
                if apply_url:
                    break

        # ── Date posted ───────────────────────────────────────────────────
        date_raw = ""
        for key in ("date posted", "age", "date", "posted"):
            if key in row_txt and row_txt[key].strip():
                date_raw = row_txt[key].strip()
                break
        date_label, date_sort = _parse_date(date_raw)

        jobs.append({
            "company":    company,
            "company_up": company.upper(),
            "role":       role or "—",
            "location":   location or "—",
            "apply_url":  apply_url,
            "source":     source_name,
            "date_label": date_label,
            "date_sort":  date_sort,
        })

    return jobs


def fetch_all_jobs(sources: list[dict]) -> list[dict]:
    """Download each GitHub markdown file and parse job rows."""
    all_jobs: list[dict] = []
    for src in sources:
        print(f"🌐  Fetching {src['name']} …", flush=True)
        try:
            resp = requests.get(src["url"], timeout=30)
            resp.raise_for_status()
            jobs = parse_markdown_jobs(resp.text, src["name"])
            print(f"   ✅  {len(jobs):,} job listings parsed.\n", flush=True)
            all_jobs.extend(jobs)
        except Exception as exc:
            print(f"   ⚠️  Failed to fetch {src['name']}: {exc}\n", flush=True)
    return all_jobs


# ── Step 3: Fuzzy match jobs → H1B employers ─────────────────────────────────

def match_jobs_to_h1b(jobs: list[dict], h1b_employers: list[str]) -> list[dict]:
    """
    For each job, fuzzy-match the company name against the H1B employer list.
    Returns only jobs where the best match score >= MATCH_THRESHOLD.
    """
    print(f"🔍  Matching {len(jobs):,} job listings against H1B employers …", flush=True)

    # Pre-build a lookup: uppercase job company → best H1B match
    unique_companies = list({j["company_up"] for j in jobs})
    matches_cache: dict[str, tuple[str, float] | None] = {}

    for company_up in unique_companies:
        # token_set_ratio handles "NVIDIA" ↔ "NVIDIA CORPORATION" correctly
        result = process.extractOne(
            company_up,
            h1b_employers,
            scorer=fuzz.token_set_ratio,
            score_cutoff=MATCH_THRESHOLD,
        )
        matches_cache[company_up] = (result[0], result[1]) if result else None

    matched_jobs: list[dict] = []
    for job in jobs:
        match = matches_cache.get(job["company_up"])
        if match:
            matched_jobs.append({**job, "h1b_match": match[0], "match_score": match[1]})

    matched_jobs.sort(key=lambda x: (-x["match_score"], x["company"]))
    print(f"   ✅  {len(matched_jobs):,} jobs at H1B-sponsoring companies found.\n", flush=True)
    return matched_jobs


# ── Step 4: Output ────────────────────────────────────────────────────────────

def print_results(matched_jobs: list[dict]) -> None:
    """Print a concise summary grouped by company."""
    if not matched_jobs:
        print("❌  No matches found. Try lowering MATCH_THRESHOLD.")
        return

    by_company: dict[str, list[dict]] = {}
    for job in matched_jobs:
        by_company.setdefault(job["company"], []).append(job)

    print("=" * 70)
    print(f"  MATCHED: {len(matched_jobs)} jobs at {len(by_company)} H1B-sponsoring companies")
    print("=" * 70)
    for company, jobs in by_company.items():
        h1b = jobs[0]["h1b_match"]
        score = jobs[0]["match_score"]
        print(f"\n🏢  {company}  (H1B: {h1b}  score={score:.0f})")
        for j in jobs:
            link = j["apply_url"] or "No link"
            print(f"   • [{j['source']}] {j['role']} | {j['location']}")
            print(f"     → {link}")
    print()


def render_html(matched_jobs: list[dict], output_path: Path) -> None:
    """Write a self-contained HTML report."""
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")

    source_colors = {
        "speedyapply/2026-SWE-College-Jobs": "#2563eb",
        "vanshb03/New-Grad-2026":            "#7c3aed",
    }

    by_company: dict[str, list[dict]] = {}
    for job in matched_jobs:
        by_company.setdefault(job["company"], []).append(job)

    # ── Recently Posted: top 20 jobs with links, sorted newest first ──────
    recent = sorted(
        [j for j in matched_jobs if j["apply_url"] and j["date_sort"] != datetime.min],
        key=lambda x: x["date_sort"],
        reverse=True,
    )[:20]

    seven_days_ago = now - timedelta(days=7)

    def _recent_card(j: dict) -> str:
        src_color = source_colors.get(j["source"], "#6b7280")
        is_new = j["date_sort"] >= seven_days_ago
        new_badge = '<span class="new-badge">NEW</span>' if is_new else ""
        return f"""
        <div class="card">
          <div class="card-header">
            <span class="card-company">{j['company']}</span>
            {new_badge}
          </div>
          <div class="card-role">{j['role']}</div>
          <div class="card-meta">
            <span>📍 {j['location']}</span>
            <span>📅 {j['date_label']}</span>
            <span class="source-tag" style="background:{src_color}">{j['source'].split('/')[0]}</span>
          </div>
          <a href="{j['apply_url']}" target="_blank" class="apply-btn">Apply ↗</a>
        </div>"""

    recent_cards_html = "".join(_recent_card(j) for j in recent)

    # ── Main table rows ───────────────────────────────────────────────────
    rows_html = ""
    for company, jobs in by_company.items():
        h1b = jobs[0]["h1b_match"]
        score = jobs[0]["match_score"]
        badge_color = "#16a34a" if score >= 95 else "#ca8a04" if score >= 88 else "#dc2626"
        for j in jobs:
            src_color = source_colors.get(j["source"], "#6b7280")
            apply_btn = (
                f'<a href="{j["apply_url"]}" target="_blank" class="apply-btn">Apply ↗</a>'
                if j["apply_url"] else '<span class="no-link">—</span>'
            )
            is_new = j["date_sort"] >= seven_days_ago and j["date_sort"] != datetime.min
            date_cell = f'{j["date_label"]} <span class="new-badge">NEW</span>' if is_new else j["date_label"]
            rows_html += f"""
            <tr>
              <td><strong>{company}</strong><br>
                  <small style="color:#6b7280">H1B: {h1b}</small></td>
              <td>{j['role']}</td>
              <td>{j['location']}</td>
              <td>{date_cell}</td>
              <td><span class="source-tag" style="background:{src_color}">{j['source']}</span></td>
              <td><span class="score-badge" style="background:{badge_color}">{score:.0f}</span></td>
              <td>{apply_btn}</td>
            </tr>"""

    source_links = " &nbsp;|&nbsp; ".join(
        f'<a href="{s["repo_url"]}" target="_blank">{s["name"]}</a>'
        for s in GITHUB_SOURCES
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>H1B Job Matcher — Results</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, sans-serif; background: #f8fafc; color: #1e293b; margin: 0; padding: 1.25rem; }}
  h1 {{ font-size: 1.6rem; margin-bottom: .25rem; }}
  h2 {{ font-size: 1.15rem; font-weight: 700; margin: 0 0 .75rem; color: #1e293b; }}
  .meta {{ color: #64748b; font-size: .875rem; margin-bottom: 1.25rem; }}

  /* Stats */
  .stats {{ display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.5rem; }}
  .stat {{ background: #fff; border: 1px solid #e2e8f0; border-radius: .5rem; padding: .75rem 1.25rem; }}
  .stat-num {{ font-size: 1.5rem; font-weight: 700; }}
  .stat-lbl {{ font-size: .75rem; color: #64748b; text-transform: uppercase; letter-spacing: .05em; }}

  /* Recently Posted section */
  .recent-section {{ background: #fff; border: 1px solid #e2e8f0; border-radius: .75rem;
                     padding: 1.1rem 1.25rem 1.25rem; margin-bottom: 1.75rem;
                     box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  .recent-section h2 {{ display: flex; align-items: center; gap: .5rem; }}
  .cards-scroll {{ display: flex; gap: .85rem; overflow-x: auto; padding-bottom: .5rem;
                   scrollbar-width: thin; }}
  .cards-scroll::-webkit-scrollbar {{ height: 5px; }}
  .cards-scroll::-webkit-scrollbar-thumb {{ background: #cbd5e1; border-radius: 9999px; }}
  .card {{ flex: 0 0 260px; border: 1px solid #e2e8f0; border-radius: .6rem;
           padding: .9rem 1rem; background: #f8fafc; display: flex;
           flex-direction: column; gap: .45rem; transition: box-shadow .15s; }}
  .card:hover {{ box-shadow: 0 4px 12px rgba(0,0,0,.1); background: #fff; }}
  .card-header {{ display: flex; align-items: center; gap: .4rem; flex-wrap: wrap; }}
  .card-company {{ font-weight: 700; font-size: .92rem; color: #0f172a; }}
  .card-role {{ font-size: .82rem; color: #334155; line-height: 1.3; flex: 1; }}
  .card-meta {{ display: flex; flex-wrap: wrap; gap: .3rem .6rem; font-size: .75rem; color: #64748b; }}
  .new-badge {{ display: inline-block; background: #16a34a; color: #fff;
                font-size: .65rem; font-weight: 700; padding: .1rem .35rem;
                border-radius: .25rem; letter-spacing: .03em; }}

  /* Filter bar */
  .filter-bar {{ display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: 1rem; }}
  .filter-bar input {{ padding: .45rem .75rem; border: 1px solid #cbd5e1; border-radius: .375rem;
                        font-size: .875rem; flex: 1; min-width: 200px; }}
  .filter-bar select {{ padding: .45rem .75rem; border: 1px solid #cbd5e1; border-radius: .375rem;
                         font-size: .875rem; }}

  /* Table */
  table {{ width: 100%; border-collapse: collapse; background: #fff;
           border-radius: .5rem; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  th {{ background: #1e293b; color: #f8fafc; text-align: left; padding: .65rem 1rem;
        font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; cursor: pointer;
        user-select: none; }}
  th:hover {{ background: #334155; }}
  td {{ padding: .65rem 1rem; border-bottom: 1px solid #f1f5f9; font-size: .875rem; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #f8fafc; }}
  .apply-btn {{ display: inline-block; background: #2563eb; color: #fff; padding: .3rem .75rem;
                border-radius: .375rem; text-decoration: none; font-size: .8rem; font-weight: 600; }}
  .apply-btn:hover {{ background: #1d4ed8; }}
  .no-link {{ color: #94a3b8; }}
  .source-tag {{ display: inline-block; color: #fff; padding: .15rem .5rem; border-radius: .25rem;
                 font-size: .7rem; font-weight: 600; white-space: nowrap; }}
  .score-badge {{ display: inline-block; color: #fff; padding: .2rem .5rem; border-radius: 9999px;
                  font-size: .75rem; font-weight: 700; }}
  .hidden {{ display: none; }}
  footer {{ margin-top: 1.5rem; font-size: .75rem; color: #94a3b8; text-align: center; }}
</style>
</head>
<body>

<h1>🗂️ H1B Sponsor Job Matcher</h1>
<p class="meta">Generated: {now_str} &nbsp;|&nbsp; Sources: {source_links}</p>

<!-- Stats -->
<div class="stats">
  <div class="stat"><div class="stat-num">{len(matched_jobs)}</div><div class="stat-lbl">Matched Jobs</div></div>
  <div class="stat"><div class="stat-num">{len(by_company)}</div><div class="stat-lbl">Companies</div></div>
  <div class="stat"><div class="stat-num">{len(recent)}</div><div class="stat-lbl">Recently Posted</div></div>
  <div class="stat"><div class="stat-num">{MATCH_THRESHOLD}</div><div class="stat-lbl">Min Match Score</div></div>
</div>

<!-- Recently Posted -->
<div class="recent-section">
  <h2>🔥 Recently Posted <span style="font-size:.8rem;font-weight:400;color:#64748b">(newest first, with apply links)</span></h2>
  <div class="cards-scroll">
    {recent_cards_html}
  </div>
</div>

<!-- Filter bar -->
<div class="filter-bar">
  <input type="text" id="search" placeholder="Search company, role, or location…" oninput="filterTable()">
  <select id="srcFilter" onchange="filterTable()">
    <option value="">All Sources</option>
    {"".join(f'<option value="{s["name"]}">{s["name"]}</option>' for s in GITHUB_SOURCES)}
  </select>
</div>

<!-- Full table -->
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
  <tbody id="tableBody">
    {rows_html}
  </tbody>
</table>

<footer>H1B data: USCIS Employer Information CSV &nbsp;|&nbsp; Jobs: GitHub (updated daily)</footer>

<script>
function filterTable() {{
  const q = document.getElementById('search').value.toLowerCase();
  const src = document.getElementById('srcFilter').value.toLowerCase();
  document.querySelectorAll('#tableBody tr').forEach(row => {{
    const text = row.textContent.toLowerCase();
    const srcMatch = !src || text.includes(src);
    row.classList.toggle('hidden', !(text.includes(q) && srcMatch));
  }});
}}

let sortDir = {{}};
function sortTable(col) {{
  const tbody = document.getElementById('tableBody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  const dir = (sortDir[col] = !(sortDir[col])) ? 1 : -1;
  rows.sort((a, b) => {{
    const ta = a.cells[col].textContent.trim();
    const tb = b.cells[col].textContent.trim();
    const na = parseFloat(ta), nb = parseFloat(tb);
    if (!isNaN(na) && !isNaN(nb)) return dir * (na - nb);
    return dir * ta.localeCompare(tb);
  }});
  rows.forEach(r => tbody.appendChild(r));
}}
</script>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    print(f"📄  HTML report saved → {output_path}\n", flush=True)


# ── Public API (used by Flask app) ────────────────────────────────────────────

def run_matcher() -> list[dict]:
    """
    Run the full pipeline and return matched jobs as JSON-serialisable dicts.
    Raises on critical failures so callers can handle errors.
    """
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"CSV not found: {CSV_PATH}")

    h1b_employers = load_h1b_employers(CSV_PATH)
    all_jobs      = fetch_all_jobs(GITHUB_SOURCES)

    if not all_jobs:
        raise RuntimeError("No job listings fetched — check internet connection.")

    matched = match_jobs_to_h1b(all_jobs, h1b_employers)

    # Serialise datetime → ISO string for JSON transport
    result = []
    for job in matched:
        d = dict(job)
        ds = d.pop("date_sort", None)
        d["date_sort_iso"] = ds.isoformat() if ds and ds != datetime.min else ""
        result.append(d)

    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n╔══════════════════════════════════════════╗")
    print("║   H1B Sponsor × New Grad Job Matcher     ║")
    print("╚══════════════════════════════════════════╝\n")

    if not CSV_PATH.exists():
        sys.exit(f"❌  CSV not found: {CSV_PATH}")

    h1b_employers = load_h1b_employers(CSV_PATH)
    all_jobs       = fetch_all_jobs(GITHUB_SOURCES)

    if not all_jobs:
        sys.exit("❌  No job listings fetched. Check your internet connection.")

    matched_jobs = match_jobs_to_h1b(all_jobs, h1b_employers)

    print_results(matched_jobs)
    render_html(matched_jobs, OUTPUT_HTML)

    print(f"Done! Open {OUTPUT_HTML} in your browser for the full interactive report.")


if __name__ == "__main__":
    main()
