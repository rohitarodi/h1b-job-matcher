#!/usr/bin/env python3
"""
Gmail Job Application Tracker
Checks Gmail for job-related emails and POSTs status updates to the H1B tracker API.
"""

import re
import requests
from datetime import datetime, timezone
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/gmail.readonly'
]
TOKEN_FILE = '/home/hpserver/openclaw-calendar/token.json'
TRACKER_API = 'http://localhost:5000/api/applications'

# Status detection rules (order matters — most specific first)
STATUS_RULES = [
    ('offer',     ['congratulations', 'pleased to offer', 'job offer', 'offer letter', 'verbal offer']),
    ('interview', ['interview', 'technical round', 'onsite', 'on-site', 'video call interview', 'schedule an interview', 'invite you to interview']),
    ('screening', ['screening', 'phone screen', 'introductory call', 'recruiter call', 'next steps', 'schedule a call', 'schedule time', 'select a time', 'book a time', 'calendly', 'preliminary']),
    ('rejected',  ['unfortunately', 'not moving forward', 'other candidates', 'not selected', 'we have decided', 'position has been filled', 'will not be moving', 'regret to inform', 'decided to move forward with other']),
    ('applied',   ['your application was sent', 'thank you for applying', 'application received', 'application has been received', 'we received your application', 'successfully applied']),
]

def detect_status(subject, snippet=''):
    text = (subject + ' ' + snippet).lower()
    for status, keywords in STATUS_RULES:
        if any(kw in text for kw in keywords):
            return status
    return None

def extract_company(subject, sender):
    """Extract company name from email subject or sender."""
    # LinkedIn pattern: "Your application was sent to COMPANY"
    m = re.search(r'(?:sent to|applying to|application (?:to|for)|at)\s+([A-Z][^,.\n]+?)(?:\s*$|\s*[,.(])', subject, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # "Thank you for applying to COMPANY"
    m = re.search(r'applying to\s+([A-Z][^\n,.(]+)', subject, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Extract from sender domain e.g. "careers@company.com"
    m = re.search(r'@([\w-]+)\.(com|io|ai|co|net|org)', sender)
    if m:
        domain = m.group(1)
        if domain not in ('gmail', 'linkedin', 'indeed', 'ziprecruiter', 'glassdoor', 'lever', 'greenhouse', 'ashby', 'smartrecruiters', 'workday', 'icims', 'taleo', 'jobvite'):
            return domain.replace('-', ' ').title()

    return None

def extract_role(subject):
    """Try to extract role from subject line."""
    # "Software Engineer at Company" or "RE: Java Developer - Company"
    m = re.search(r'(?:for|re:|position:|role:)?\s*([A-Za-z\s/]+(?:Engineer|Developer|Analyst|Manager|Designer|Scientist|Architect|Lead|Intern|Associate|Consultant|Specialist|DevOps|SRE|Platform|Cloud|Backend|Frontend|Full Stack)[A-Za-z\s/]*)', subject, re.IGNORECASE)
    if m:
        return m.group(1).strip()[:100]
    return None

def get_gmail_service():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build('gmail', 'v1', credentials=creds)

def fetch_job_emails(service, max_results=50):
    keywords = [
        '"your application was sent"',
        '"thank you for applying"',
        '"application received"',
        '"interview"',
        '"screening"',
        '"unfortunately"',
        '"not moving forward"',
        '"offer letter"',
        '"next steps"',
    ]
    query = f'({" OR ".join(keywords)})'
    result = service.users().messages().list(
        userId='me',
        q=query,
        maxResults=max_results
    ).execute()
    return result.get('messages', [])

def process_emails():
    service = get_gmail_service()
    messages = fetch_job_emails(service)

    updates = []
    for msg in messages:
        m = service.users().messages().get(
            userId='me', id=msg['id'],
            format='metadata',
            metadataHeaders=['Subject', 'From', 'Date']
        ).execute()

        headers = {h['name']: h['value'] for h in m['payload']['headers']}
        subject = headers.get('Subject', '')
        sender  = headers.get('From', '')
        date    = headers.get('Date', '')
        snippet = m.get('snippet', '')

        status = detect_status(subject, snippet)
        if not status:
            continue

        company = extract_company(subject, sender)
        if not company:
            continue

        role = extract_role(subject)

        updates.append({
            'company': company,
            'role': role,
            'status': status,
            'email_subject': subject[:200],
            'source': 'gmail',
            'applied_date': date[:30] if status == 'applied' else None,
        })

    return updates

def post_to_tracker(updates):
    sent = 0
    alerts = []
    for update in updates:
        try:
            r = requests.post(TRACKER_API, json=update, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if data.get('is_new') or data.get('status_changed'):
                    alerts.append(data)
                sent += 1
        except Exception as e:
            print(f'Error posting {update["company"]}: {e}')
    return sent, alerts

if __name__ == '__main__':
    print(f'[{datetime.now():%H:%M:%S}] Fetching job emails...')
    updates = process_emails()
    print(f'Found {len(updates)} job-related emails')

    if not updates:
        print('NO_ALERTS')
    else:
        sent, alerts = post_to_tracker(updates)
        print(f'Posted {sent} updates to tracker')

        if alerts:
            print('\n=== IMPORTANT UPDATES ===')
            for a in alerts:
                print(f"{'🆕 NEW' if a.get('is_new') else '🔄 UPDATE'}: {a['company']} → {a['status'].upper()}")
        else:
            print('No new status changes')
