# H1B Job Matcher

Matches H1B-sponsoring companies from USCIS data with live new grad job listings from GitHub — built with Python, Flask, and Docker.

![Python](https://img.shields.io/badge/Python-3.13-blue) ![Flask](https://img.shields.io/badge/Flask-3.0-green) ![Docker](https://img.shields.io/badge/Docker-ready-2496ED)

---

## What it does

1. **Loads** the USCIS H1B Employer Information CSV (22,000+ sponsoring companies)
2. **Fetches** live new grad job listings daily from:
   - [speedyapply/2026-SWE-College-Jobs](https://github.com/speedyapply/2026-SWE-College-Jobs)
   - [vanshb03/New-Grad-2026](https://github.com/vanshb03/New-Grad-2026)
3. **Fuzzy-matches** company names between both sources
4. **Serves** a live web dashboard at `http://localhost:5000`

---

## Features

- 🔥 **Recently Posted** — horizontal card strip showing newest jobs first with NEW badges
- 🏢 **Sponsoring Companies** — clickable chips for every matched company, click to filter the table
- 🔍 **Search & Filter** — filter by company, role, location, or source
- ↕️ **Sortable table** — sort by any column
- ↻ **Reload Now** button — fetch fresh data on demand
- ⏱ **Auto-refresh** — re-runs automatically every hour
- 🟢 **Live status indicator** — shows last updated time and next refresh

---

## Quick Start (Docker)

```bash
docker pull arodirohit/h1b-job-matcher:latest

docker run -d \
  -p 5000:5000 \
  --restart unless-stopped \
  --name h1b \
  arodirohit/h1b-job-matcher:latest
```

Open **`http://localhost:5000`** — data loads in ~30 seconds on first run.

---

## Run Locally

**Requirements**: Python 3.13+

```bash
git clone https://github.com/rohitarodi/h1b-job-matcher.git
cd h1b-job-matcher

pip install -r requirements.txt

# Add your USCIS CSV file
# Download from: https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub
cp "Employer Information.csv" .

python -X utf8 app.py
```

Open **`http://localhost:5000`**

---

## Data Sources

| Source | Description |
|--------|-------------|
| [USCIS H1B Employer Data Hub](https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub) | Official list of companies that have sponsored H1B visas |
| [speedyapply/2026-SWE-College-Jobs](https://github.com/speedyapply/2026-SWE-College-Jobs) | Curated new grad SWE jobs, updated daily |
| [vanshb03/New-Grad-2026](https://github.com/vanshb03/New-Grad-2026) | Community-maintained new grad job listings |

---

## Project Structure

```
h1b-job-matcher/
├── app.py                    # Flask web server + frontend SPA
├── h1b_matcher.py            # Core matching logic
├── Dockerfile                # Docker build config
├── requirements.txt          # Python dependencies
└── Employer Information.csv  # USCIS data (not in repo — add manually)
```

---

## Docker Hub

```
arodirohit/h1b-job-matcher:latest
```

---

## Update to Latest

```bash
docker pull arodirohit/h1b-job-matcher:latest
docker stop h1b && docker rm h1b
docker run -d -p 5000:5000 --restart unless-stopped --name h1b arodirohit/h1b-job-matcher:latest
```
