# Application Tracker Setup

The H1B Job Matcher includes a built-in **Application Tracker** that automatically
syncs your job applications from Gmail and visualizes them as an interactive pipeline tree.

## How it works

```
Gmail (every 30 min)
    ↓
gmail_tracker.py  ← runs on your host machine
  • Detects: Applied / Screening / Interview / Offer / Rejected
  • POSTs updates → http://localhost:5000/api/applications
    ↓
Flask app (Docker container)
  • Stores in SQLite (persisted via Docker volume)
  • Serves tracker UI at http://localhost:5000 → "Application Tracker" tab
```

## Prerequisites

- Docker + Docker Compose
- Python 3.10+
- Google OAuth token with `gmail.readonly` + `calendar` scopes

## Setup

### 1. Start the container with persistence

```bash
docker compose up -d
```

> This uses a named Docker volume (`tracker_data`) so your DB survives container rebuilds.

### 2. Set up Gmail OAuth

Follow the [Google OAuth setup guide](https://github.com/rohitarodi/h1b-job-matcher#oauth-setup)
to generate a `token.json` with `gmail.readonly` scope.

### 3. Install Python dependencies

```bash
pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client requests
```

### 4. Run the tracker manually (test)

```bash
python3 gmail_tracker.py
```

You should see detected applications posted to the tracker.

### 5. Schedule with cron (every 30 min)

```bash
crontab -e
```

Add:
```
*/30 * * * * cd /path/to/your/token && python3 /path/to/gmail_tracker.py >> /tmp/tracker.log 2>&1
```

## Configuration

Edit `gmail_tracker.py` top section:

```python
TOKEN_FILE  = '/path/to/token.json'   # your Google OAuth token
TRACKER_API = 'http://localhost:5000/api/applications'
```

## Tracker UI

Open `http://localhost:5000` → click **📊 Application Tracker** tab.

- **Stats bar** — total applied, screening, interviews, offers, rejections
- **Pipeline tree** — click any node to see company names at that stage
- **Applications table** — searchable, filterable full list

## Status detection keywords

| Status     | Detected from                                              |
|------------|------------------------------------------------------------|
| Applied    | "your application was sent", "thank you for applying"      |
| Screening  | "screening", "next steps", "schedule a call", "calendly"   |
| Interview  | "interview", "technical round", "onsite"                   |
| Offer      | "congratulations", "offer letter", "pleased to offer"      |
| Rejected   | "unfortunately", "not moving forward", "other candidates"  |

## API endpoints

| Method | Endpoint                  | Description                    |
|--------|---------------------------|--------------------------------|
| GET    | `/api/applications`       | All tracked applications       |
| POST   | `/api/applications`       | Upsert an application          |
| GET    | `/api/tracker/tree`       | Tree data for D3 visualization |
