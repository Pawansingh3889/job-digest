"""Watch the inbox for application outcomes and job-alert emails.

  python mailwatch.py            # one pass over the last 7 days
  python mailwatch.py --watch    # poll every 2 minutes, Ctrl+C stops

Needs a Gmail app password in config.local.json (the same block SMTP uses):

  { "email": { "username": "you@gmail.com", "password": "app-password" } }

What it does, conservatively:
- ATS confirmation email matching a picked application  -> stage moves to applied
- rejection wording matching a tracked application      -> printed for you to confirm
- job-alert emails (LinkedIn, Indeed)                   -> job links extracted and printed
Nothing is deleted, sent, or marked read; the mailbox is opened read-only.
"""

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import email
import email.policy
import imaplib
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "seen.db"

ATS_SENDERS = ("greenhouse.io", "lever.co", "hibob.com", "ashbyhq.com",
               "workable.com", "myworkdayjobs.com", "smartrecruiters.com",
               "teamtailor.com", "bamboohr.com", "ecotricity.co.uk", "indeed.com")
ALERT_SENDERS = ("jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com",
                 "alert@indeed.com", "noreply@jobicy.com")
REJECT_WORDS = ("unfortunately", "not be taking", "regret to inform",
                "not been selected", "unsuccessful", "other candidates")
CONFIRM_WORDS = ("thank you for applying", "thanks for applying",
                 "application received", "we received your application",
                 "your application for", "thank you for your application")

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def creds():
    local = HERE / "config.local.json"
    if not local.exists():
        sys.exit("config.local.json missing; add email.username and email.password (Gmail app password)")
    cfg = json.loads(local.read_text(encoding="utf-8")).get("email", {})
    if not (cfg.get("username") and cfg.get("password")):
        sys.exit("email.username / email.password missing in config.local.json "
                 "(create a Gmail app password: Google account, Security, 2-Step Verification, App passwords)")
    return cfg["username"], cfg["password"]


def tracked_apps(con):
    try:
        return con.execute("SELECT slug, company, title, stage FROM apps").fetchall()
    except sqlite3.OperationalError:
        return []


def company_token(company):
    token = re.split(r"[\s(/,]+", (company or "").lower().strip())
    return token[0] if token and token[0] else None


def one_pass(verbose=True):
    user, password = creds()
    con = sqlite3.connect(DB)
    apps = tracked_apps(con)
    moved, rejections, alerts = [], [], []

    box = imaplib.IMAP4_SSL("imap.gmail.com")
    box.login(user, password)
    box.select("INBOX", readonly=True)
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%d-%b-%Y")
    _, data = box.search(None, f'(SINCE "{since}")')
    ids = data[0].split()

    for mid in ids[-300:]:
        _, msg_data = box.fetch(mid, "(BODY.PEEK[])")
        msg = email.message_from_bytes(msg_data[0][1], policy=email.policy.default)
        sender = (msg.get("From") or "").lower()
        subject = (msg.get("Subject") or "").lower()

        if any(s in sender for s in ALERT_SENDERS):
            body = msg.get_body(preferencelist=("plain", "html"))
            text = body.get_content() if body else ""
            links = re.findall(r"https://(?:www\.)?linkedin\.com/(?:comm/)?jobs/view/\d+|https://(?:uk|www)\.indeed\.com/viewjob\?jk=[a-z0-9]+", text)
            for link in dict.fromkeys(links):
                alerts.append((subject[:60], link.replace("/comm/", "/")))
            continue

        if not any(s in sender for s in ATS_SENDERS) and "application" not in subject:
            continue

        haystack = sender + " " + subject
        for slug, company, title, stage in apps:
            token = company_token(company)
            if not token or token not in haystack:
                continue
            if any(w in subject for w in REJECT_WORDS):
                rejections.append((slug, company, subject[:70]))
            elif stage == "picked" and any(w in subject for w in CONFIRM_WORDS):
                con.execute("UPDATE apps SET stage='applied', updated=? WHERE slug=?",
                            (datetime.now(timezone.utc).isoformat(), slug))
                con.commit()
                moved.append((slug, company))
    box.logout()

    if verbose or moved or rejections or alerts:
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] scanned {min(len(ids),300)} messages")
        for slug, company in moved:
            print(f"  CONFIRMED  {company}: moved to applied ({slug})")
        for slug, company, subj in rejections:
            print(f"  REJECTION? {company}: \"{subj}\" -> confirm with: python apply.py rejected {slug}")
        for subj, link in alerts[:15]:
            print(f"  ALERT      {subj}")
            print(f"             python apply.py pick {link}")
        if not (moved or rejections or alerts):
            print("  nothing new for the funnel")
    return moved, rejections, alerts


def main():
    if "--watch" in sys.argv:
        print("watching the inbox every 120s, Ctrl+C stops")
        while True:
            try:
                one_pass(verbose=False)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print("pass failed:", str(exc)[:120])
            try:
                time.sleep(120)
            except KeyboardInterrupt:
                print("\nstopped")
                return
    else:
        one_pass()


if __name__ == "__main__":
    main()
