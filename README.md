# H1B Job Matcher + Application Tracker

Matches H1B-sponsoring companies from USCIS data with live new grad job listings from GitHub — built with Python, Flask, and Docker. Now includes a **Gmail-powered application tracker** with an interactive pipeline tree.

![Python](https://img.shields.io/badge/Python-3.13-blue) ![Flask](https://img.shields.io/badge/Flask-3.0-green) ![Docker](https://img.shields.io/badge/Docker-ready-2496ED)

---

## What it does

1. **Loads** the USCIS H1B Employer Information CSV (22,000+ sponsoring companies)
2. **Fetches** live new grad job listings daily from:
   - [speedyapply/2026-SWE-College-Jobs](https://github.com/speedyapply/2026-SWE-College-Jobs)
   - [vanshb03/New-Grad-2026](https://github.com/vanshb03/New-Grad-2026)
3. **Fuzzy-matches** company names between both sources
4. **Serves** a live web dashboard at `http://localhost:5000`
5. **Tracks** your job applications automatically from Gmail

---

## Features

### 💼 Job Listings Tab
- 🔥 **Recently Posted** — horizontal card strip showing newest jobs first with NEW badges
- 🏢 **Sponsoring Companies** — clickable chips for every matched company, click to filter
- 🔍 **Search & Filter** — filter by company, role, location, or source
- ↕️ **Sortable table** — sort by any column
- ↻ **Reload Now** button — fetch fresh data on demand
- ⏱ **Auto-refresh** — re-runs automatically every hour
- 🟢 **Live status indicator** — shows last updated time and next refresh

### 📊 Application Tracker Tab
- 🌳 **Interactive D3 pipeline tree** — visualize your funnel: Applied → Screening → Interview → Offer
- 📋 **Applications table** — searchable, filterable list of all tracked applications
- 📧 **Gmail auto-sync** — detects applied/screening/interview/offer/rejected from your inbox
- 💾 **Persistent SQLite DB** — survives container rebuilds via Docker volume

---

## Quick Start (Docker — Job Matcher only)

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

## Quick Start with Application Tracker (Docker Compose)

This is the recommended setup — it persists your tracker DB across rebuilds.

```bash
git clone https://github.com/rohitarodi/h1b-job-matcher.git
cd h1b-job-matcher
docker compose up -d
```

Open **`http://localhost:5000`** → click **📊 Application Tracker** tab.

---

## Setting Up Gmail Auto-Sync

The tracker reads your Gmail to automatically detect job application statuses.

### 1. Enable Gmail API

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project (or use an existing one)
3. **APIs & Services → Library** → search **"Gmail API"** → Enable
4. Also enable **Google Calendar API** (same flow)
5. **APIs & Services → Credentials → + Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Download the `credentials.json`

### 2. Add OAuth scopes

**APIs & Services → OAuth consent screen → Edit App → Scopes**, add:
- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/calendar`

Under **Test users**, add your Gmail address.

### 3. Generate your token

```bash
pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client requests

# Put your credentials.json in a folder, then:
python3 auth.py
```

> Follow the browser prompt to authorize. A `token.json` will be saved.

The `auth.py` script:
```python
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/gmail.readonly'
]
flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
creds = flow.run_local_server(port=8888, open_browser=False)
with open('token.json', 'w') as f:
    f.write(creds.to_json())
print('Done! token.json saved.')
```

### 4. Configure gmail_tracker.py

Edit the top of `gmail_tracker.py`:

```python
TOKEN_FILE  = '/path/to/your/token.json'
TRACKER_API = 'http://localhost:5000/api/applications'
```

### 5. Run manually to test

```bash
python3 gmail_tracker.py
```

You should see detected applications synced to the tracker.

### 6. Schedule with cron (every 30 min)

```bash
crontab -e
```

Add:
```
*/30 * * * * cd /path/to/token/folder && python3 /path/to/gmail_tracker.py >> /tmp/tracker.log 2>&1
```

---

## Status Detection

The tracker automatically detects status from email subject + snippet:

| Status     | Detected from                                                    |
|------------|------------------------------------------------------------------|
| `applied`    | "your application was sent", "thank you for applying"          |
| `screening`  | "screening", "next steps", "schedule a call", "calendly"       |
| `interview`  | "interview", "technical round", "onsite"                       |
| `offer`      | "congratulations", "offer letter", "pleased to offer"          |
| `rejected`   | "unfortunately", "not moving forward", "other candidates"      |

Status only **upgrades** (applied → screening → interview → offer). Rejections can occur at any stage.

---

## Run Locally (without Docker)

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

## API Endpoints

| Method | Endpoint                  | Description                        |
|--------|---------------------------|------------------------------------|
| GET    | `/api/jobs`               | All matched job listings            |
| GET    | `/api/status`             | Matcher cache status               |
| POST   | `/api/reload`             | Trigger immediate job refresh      |
| GET    | `/api/applications`       | All tracked applications           |
| POST   | `/api/applications`       | Upsert an application              |
| GET    | `/api/tracker/tree`       | Tree data for D3 visualization     |

---

## Data Sources

| Source | Description |
|--------|-------------|
| [USCIS H1B Employer Data Hub](https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub) | Official list of H1B sponsoring companies |
| [speedyapply/2026-SWE-College-Jobs](https://github.com/speedyapply/2026-SWE-College-Jobs) | Curated new grad SWE jobs, updated daily |
| [vanshb03/New-Grad-2026](https://github.com/vanshb03/New-Grad-2026) | Community-maintained new grad job listings |

---

## Project Structure

```
h1b-job-matcher/
├── app.py                    # Flask web server + frontend SPA + tracker API
├── h1b_matcher.py            # Core matching logic
├── gmail_tracker.py          # Gmail → tracker sync script (run on host)
├── auth.py                   # Google OAuth token generator
├── docker-compose.yml        # Recommended: includes DB volume persistence
├── Dockerfile                # Docker build config
├── requirements.txt          # Python dependencies
├── .env.example              # Environment variable reference
├── TRACKER_SETUP.md          # Detailed tracker setup guide
└── Employer Information.csv  # USCIS data (not in repo — add manually)
```

---

## Docker Hub

```
arodirohit/h1b-job-matcher:latest
```

## Update to Latest

```bash
docker compose pull && docker compose up -d
```
