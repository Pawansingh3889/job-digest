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


def parse_alert_jobs(html):
    """Pull (title, url) pairs out of a job-alert email body.
    Matches LinkedIn jobs/view and Indeed viewjob links, using the anchor
    text as the title where present."""
    jobs = []
    seen = set()
    pattern = re.compile(
        r'<a[^>]+href="([^"]*(?:linkedin\.com/(?:comm/)?jobs/view/\d+|indeed\.com/(?:viewjob|rc/clk))[^"]*)"[^>]*>(.*?)</a>',
        re.I | re.S,
    )
    for url, inner in pattern.findall(html):
        url = url.split("?")[0].replace("/comm/", "/").rstrip("/")
        if url in seen:
            continue
        title = re.sub(r"<[^>]+>", " ", inner)
        title = re.sub(r"\s+", " ", title).strip()
        if not title or len(title) < 3 or title.lower() in ("view job", "apply now", "see job"):
            title = "(LinkedIn role, open the link)"
        jobs.append((title[:120], url))
        seen.add(url)
    return jobs


def harvest(verbose=True):
    """Read job-alert emails and seed the roles into the digest inbox so they
    appear in `apply.py list` and the dashboard next to board jobs. No scraping:
    the jobs came to you, in email you asked LinkedIn to send."""
    user, password = creds()
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS seen (url TEXT PRIMARY KEY, first_seen TEXT, title TEXT, company TEXT, payload TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS shown (run_date TEXT, url TEXT, score INT, PRIMARY KEY (run_date, url))")

    box = imaplib.IMAP4_SSL("imap.gmail.com")
    box.login(user, password)
    # All Mail rather than INBOX: filter the raw alert emails straight to a
    # label to keep your inbox clean, and the harvest still finds them
    status, _ = box.select('"[Gmail]/All Mail"', readonly=True)
    if status != "OK":
        box.select("INBOX", readonly=True)
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%d-%b-%Y")
    _, data = box.search(None, f'(SINCE "{since}")')
    ids = data[0].split()

    found, added = [], 0
    stamp = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()
    for mid in ids[-300:]:
        _, msg_data = box.fetch(mid, "(BODY.PEEK[])")
        msg = email.message_from_bytes(msg_data[0][1], policy=email.policy.default)
        sender = (msg.get("From") or "").lower()
        if not any(s in sender for s in ALERT_SENDERS):
            continue
        subject = (msg.get("Subject") or "").strip()
        body = msg.get_body(preferencelist=("html", "plain"))
        html = body.get_content() if body else ""
        source = "linkedin-alert" if "linkedin" in sender else "indeed-alert"
        for title, url in parse_alert_jobs(html):
            if title.startswith("(") and subject:
                # no anchor text. The subject names only the headline role;
                # the other links are "similar jobs", so claiming the subject
                # as this link's title would fabricate identities. Keep it as
                # context only.
                title = f"(from the alert: {subject[:70]})"
            known = con.execute("SELECT 1 FROM seen WHERE url = ?", (url,)).fetchone()
            payload = json.dumps({"source": source, "title": title, "company": "",
                                  "location": "", "salary": "", "url": url,
                                  "posted": None, "description": ""})
            con.execute("INSERT OR IGNORE INTO seen (url, first_seen, title, company, payload) VALUES (?,?,?,?,?)",
                        (url, now, title, "", payload))
            if known:  # refresh a stored placeholder, never a real title
                con.execute(
                    "UPDATE seen SET title = ?, payload = ? "
                    "WHERE url = ? AND title LIKE '(%'",
                    (title, payload, url))
            con.execute("INSERT OR IGNORE INTO shown (run_date, url, score) VALUES (?,?,?)",
                        (stamp, url, 40))
            if not known:
                added += 1
                found.append((source, title, url))
    con.commit()
    con.close()
    box.logout()

    if verbose or found:
        print(f"harvest: {added} new job-alert roles seeded into the inbox")
        for source, title, url in found[:20]:
            print(f"  [{source}] {title}")
        if added:
            print("\nthey are now in `apply.py list` and the dashboard.")
            print("LinkedIn job pages need a login, so a pack has no JD until you add it:")
            print("pick one, then paste the description into its job.md (or read it in a")
            print("Claude session with the browser extension), then tailor and fill as usual.")
    return found


def one_pass(verbose=True):
    user, password = creds()
    con = sqlite3.connect(DB)
    apps = tracked_apps(con)
    moved, rejections, alerts, ambiguous = [], [], [], []

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
        confirm_hit = any(w in subject for w in CONFIRM_WORDS)
        candidates = []
        for slug, company, title, stage in apps:
            token = company_token(company)
            if not token or token not in haystack:
                continue
            if any(w in subject for w in REJECT_WORDS):
                rejections.append((slug, company, subject[:70]))
            elif stage == "picked" and confirm_hit:
                candidates.append((slug, company, title))
        chosen = candidates
        if len(chosen) > 1:
            # one confirmation, several packs at the same company: only a
            # title word unique to one pack may decide, else touch nothing
            toks = {s: set(re.findall(r"[a-z]{3,}", (t or "").lower()))
                    for s, _c, t in chosen}
            winners = []
            for cand in chosen:
                others = set().union(*(toks[o[0]] for o in chosen if o[0] != cand[0]))
                if any(w in subject for w in toks[cand[0]] - others):
                    winners.append(cand)
            chosen = winners if len(winners) == 1 else []
            if not chosen:
                ambiguous.append((subject[:70], [s for s, _c, _t in candidates]))
        for slug, company, _title in chosen:
            con.execute("UPDATE apps SET stage='applied', updated=? WHERE slug=?",
                        (datetime.now(timezone.utc).isoformat(), slug))
            con.commit()
            moved.append((slug, company))
    box.logout()

    if verbose or moved or rejections or alerts or ambiguous:
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] scanned {min(len(ids),300)} messages")
        for slug, company in moved:
            print(f"  CONFIRMED  {company}: moved to applied ({slug})")
        for slug, company, subj in rejections:
            print(f"  REJECTION? {company}: \"{subj}\" -> confirm with: python apply.py rejected {slug}")
        for subj, slugs in ambiguous:
            print(f"  AMBIGUOUS  \"{subj}\" matches several packs; nothing moved. If one was really submitted:")
            for s in slugs:
                print(f"             python apply.py applied {s}")
        for subj, link in alerts[:15]:
            print(f"  ALERT      {subj}")
            print(f"             python apply.py pick {link}")
        if not (moved or rejections or alerts or ambiguous):
            print("  nothing new for the funnel")
    return moved, rejections, alerts


def main():
    if "--harvest" in sys.argv:
        harvest()
    elif "--watch" in sys.argv:
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
