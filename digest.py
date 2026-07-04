"""Daily job digest: fetch UK-friendly remote data roles from legitimate job APIs,
dedupe against everything already seen, score against a keyword profile, and
write a local HTML digest. Optionally email it if SMTP is configured.

No scraping, no cloud database, no auto-generated CVs. Sources are official
APIs and public feeds; state is a local SQLite file; the digest is a file on
disk that can also be emailed to you.
"""

try:  # use the OS trust store when available; needed behind TLS-inspecting AV/proxies
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import argparse
import html
import json
import re
import sqlite3
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

import requests

HERE = Path(__file__).resolve().parent
UA = {"User-Agent": "job-digest/0.1 (personal job search tool)"}
TIMEOUT = 30

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_config() -> dict:
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    local = HERE / "config.local.json"
    if local.exists():
        overlay = json.loads(local.read_text(encoding="utf-8"))
        for key, value in overlay.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?(li|p|div|h[1-6])[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_date(value: str) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        pass
    match = re.match(r"(\d{2})/(\d{2})/(\d{4})", value)  # Reed: DD/MM/YYYY
    if match:
        day, month, year = map(int, match.groups())
        return datetime(year, month, day, tzinfo=timezone.utc)
    return None


def norm(source, title, company, location, salary, url, posted, description):
    return {
        "source": source,
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "").strip(),
        "salary": (salary or "").strip(),
        "url": (url or "").strip(),
        "posted": posted,
        "description": strip_html(description or "")[:4000],
    }


# ---------------------------------------------------------------- fetchers

def fetch_remotive(cfg, errors):
    jobs = []
    for query in cfg["queries"]:
        try:
            r = requests.get(
                "https://remotive.com/api/remote-jobs",
                params={"search": query, "limit": 50},
                headers=UA,
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            for j in r.json().get("jobs", []):
                jobs.append(norm(
                    "remotive", j.get("title"), j.get("company_name"),
                    j.get("candidate_required_location"), j.get("salary"),
                    j.get("url"), parse_date(j.get("publication_date", "")),
                    j.get("description"),
                ))
        except Exception as exc:
            errors.append(f"remotive ({query}): {exc}")
    return jobs


def fetch_remoteok(cfg, errors):
    jobs = []
    try:
        r = requests.get("https://remoteok.com/api", headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        for j in r.json():
            if not isinstance(j, dict) or not j.get("position"):
                continue  # first element is a legal notice
            salary = ""
            if j.get("salary_min") and j.get("salary_max"):
                salary = f"${j['salary_min']:,}-${j['salary_max']:,}"
            jobs.append(norm(
                "remoteok", j.get("position"), j.get("company"),
                j.get("location"), salary, j.get("url"),
                parse_date(j.get("date", "")),
                (j.get("description") or "") + " " + " ".join(j.get("tags") or []),
            ))
    except Exception as exc:
        errors.append(f"remoteok: {exc}")
    return jobs


def fetch_wwr(cfg, errors):
    jobs = []
    for slug in cfg.get("wwr_feeds", []):
        try:
            r = requests.get(
                f"https://weworkremotely.com/categories/{slug}.rss",
                headers=UA, timeout=TIMEOUT,
            )
            r.raise_for_status()
            root = ElementTree.fromstring(r.content)
            for item in root.iter("item"):
                raw_title = item.findtext("title") or ""
                company, _, title = raw_title.partition(":")
                if not title:
                    company, title = "", raw_title
                region = item.findtext("region") or ""
                jobs.append(norm(
                    "weworkremotely", title, company, region, "",
                    item.findtext("link"), parse_date(item.findtext("pubDate") or ""),
                    item.findtext("description"),
                ))
        except Exception as exc:
            errors.append(f"weworkremotely ({slug}): {exc}")
    return jobs


def fetch_adzuna(cfg, errors):
    creds = cfg.get("adzuna", {})
    if not (creds.get("app_id") and creds.get("app_key")):
        return []
    jobs = []
    for query in cfg["queries"]:
        try:
            r = requests.get(
                "https://api.adzuna.com/v1/api/jobs/gb/search/1",
                params={
                    "app_id": creds["app_id"], "app_key": creds["app_key"],
                    "what": query, "results_per_page": 50,
                    "max_days_old": cfg.get("days_back", 7), "sort_by": "date",
                },
                headers=UA, timeout=TIMEOUT,
            )
            r.raise_for_status()
            for j in r.json().get("results", []):
                salary = ""
                if j.get("salary_min"):
                    salary = f"GBP {int(j['salary_min']):,}-{int(j.get('salary_max') or j['salary_min']):,}"
                jobs.append(norm(
                    "adzuna", j.get("title"), (j.get("company") or {}).get("display_name"),
                    (j.get("location") or {}).get("display_name"), salary,
                    j.get("redirect_url"), parse_date(j.get("created", "")),
                    j.get("description"),
                ))
        except Exception as exc:
            errors.append(f"adzuna ({query}): {exc}")
    return jobs


def fetch_reed(cfg, errors):
    creds = cfg.get("reed", {})
    if not creds.get("api_key"):
        return []
    jobs = []
    for query in cfg["queries"]:
        try:
            r = requests.get(
                "https://www.reed.co.uk/api/1.0/search",
                params={"keywords": query, "resultsToTake": 50},
                auth=(creds["api_key"], ""),
                headers=UA, timeout=TIMEOUT,
            )
            r.raise_for_status()
            for j in r.json().get("results", []):
                salary = ""
                if j.get("minimumSalary"):
                    salary = f"GBP {int(j['minimumSalary']):,}-{int(j.get('maximumSalary') or j['minimumSalary']):,}"
                jobs.append(norm(
                    "reed", j.get("jobTitle"), j.get("employerName"),
                    j.get("locationName"), salary, j.get("jobUrl"),
                    parse_date(j.get("date", "")), j.get("jobDescription"),
                ))
        except Exception as exc:
            errors.append(f"reed ({query}): {exc}")
    return jobs


UK_SOURCES = {"adzuna", "reed"}          # UK by construction
REMOTE_SOURCES = {"remotive", "remoteok", "weworkremotely"}  # remote by construction


# ---------------------------------------------------------------- filtering

def eligible(job, cfg) -> bool:
    haystack = " ".join([job["location"], job["title"], job["description"]]).lower()
    for phrase in cfg["negative_locations"]:
        if phrase in haystack and not any(m in haystack for m in ("united kingdom", " uk", "uk ", "europe", "emea")):
            return False
    if job["source"] in UK_SOURCES:
        return True
    loc = job["location"].lower()
    if not loc:
        return True  # remote board, unspecified = usually worldwide
    return any(marker in loc for marker in cfg["accept_locations"])


def score(job, cfg):
    text = (job["title"] + " " + job["description"]).lower()
    title = job["title"].lower()
    points, matched = 0, []
    for term in cfg["title_terms"]:
        if term in title:
            points += 25
            matched.append(term)
    for term in cfg["boost_terms"]:
        if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text):
            points += 6
            matched.append(term)
    for term in cfg["negative_terms"]:
        if term in text:
            points -= 30
            matched.append("NOT:" + term)
    if job["salary"]:
        points += 5
    # remote is a priority, not a gate: remote > hybrid > on-site, all listed
    loc_text = " ".join([job["location"], job["title"], job["description"][:500]]).lower()
    if job["source"] in REMOTE_SOURCES or any(
        m in loc_text for m in ("remote", "work from home", "anywhere", "worldwide")
    ):
        points += cfg.get("remote_bonus", 15)
        matched.append("remote")
    elif "hybrid" in loc_text:
        points += cfg.get("hybrid_bonus", 8)
        matched.append("hybrid")
    return points, matched


# ---------------------------------------------------------------- dedupe

def split_new(jobs, db_path):
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS seen (url TEXT PRIMARY KEY, first_seen TEXT, title TEXT, company TEXT)"
    )
    try:  # payload added later; migrate old databases in place
        con.execute("ALTER TABLE seen ADD COLUMN payload TEXT")
    except sqlite3.OperationalError:
        pass
    con.execute(
        "CREATE TABLE IF NOT EXISTS shown (run_date TEXT, url TEXT, score INT, PRIMARY KEY (run_date, url))"
    )
    fresh = []
    for job in jobs:
        if not job["url"]:
            continue
        known = con.execute("SELECT 1 FROM seen WHERE url = ?", (job["url"],)).fetchone()
        if not known:
            fresh.append(job)
    now = datetime.now(timezone.utc).isoformat()
    con.executemany(
        "INSERT OR IGNORE INTO seen (url, first_seen, title, company, payload) VALUES (?, ?, ?, ?, ?)",
        [
            (j["url"], now, j["title"], j["company"],
             json.dumps({**j, "posted": j["posted"].isoformat() if j["posted"] else None}))
            for j in jobs if j["url"]
        ],
    )
    con.commit()
    con.close()
    return fresh


def record_shown(rows, db_path):
    con = sqlite3.connect(db_path)
    stamp = datetime.now().strftime("%Y-%m-%d")
    con.executemany(
        "INSERT OR REPLACE INTO shown (run_date, url, score) VALUES (?, ?, ?)",
        [(stamp, job["url"], points) for job, points, _ in rows],
    )
    con.commit()
    con.close()


# ---------------------------------------------------------------- output

def build_html(rows, errors, counts, cfg):
    e = html.escape
    today = datetime.now().strftime("%A %d %B %Y")
    parts = [
        "<meta charset='utf-8'><title>Job digest</title>",
        "<div style='font-family:Segoe UI,Arial,sans-serif;max-width:860px;margin:24px auto;color:#1a1d29;'>",
        f"<h2 style='margin-bottom:2px;'>Job digest, {e(today)}</h2>",
        f"<p style='color:#666;margin-top:0;'>{len(rows)} new since last run. "
        + " · ".join(f"{k}: {v}" for k, v in counts.items()) + "</p>",
    ]
    for job, points, matched in rows:
        terms = ", ".join(t for t in matched if not t.startswith("NOT:"))[:160]
        salary = f" · {e(job['salary'])}" if job["salary"] else ""
        posted = job["posted"].strftime("%d %b") if job["posted"] else ""
        parts.append(
            "<div style='border:1px solid #e3e6ef;border-radius:8px;padding:14px 16px;margin:10px 0;'>"
            f"<div style='font-size:16px;font-weight:600;'><a href='{e(job['url'])}' "
            f"style='color:#0a66c2;text-decoration:none;'>{e(job['title'])}</a></div>"
            f"<div style='color:#444;margin-top:2px;'>{e(job['company'])} · {e(job['location'] or 'remote')}{salary}</div>"
            f"<div style='color:#888;font-size:12px;margin-top:6px;'>score {points} · {e(job['source'])}"
            + (f" · posted {posted}" if posted else "")
            + (f"<br>matched: {e(terms)}" if terms else "")
            + "</div></div>"
        )
    if not rows:
        parts.append("<p>Nothing new above the score threshold today.</p>")
    if errors:
        parts.append(
            "<p style='color:#a33;font-size:12px;'>Source errors: " + e("; ".join(errors)) + "</p>"
        )
    parts.append("<p style='color:#aaa;font-size:11px;'>job-digest, local and free. State: seen.db</p></div>")
    return "\n".join(parts)


def send_email(cfg, subject, body_html, errors):
    email_cfg = cfg.get("email", {})
    required = ("smtp_host", "username", "password", "to")
    if not all(email_cfg.get(k) for k in required):
        return False
    try:
        import smtplib

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = email_cfg["username"]
        msg["To"] = email_cfg["to"]
        msg.attach(MIMEText(body_html, "html", "utf-8"))
        with smtplib.SMTP(email_cfg["smtp_host"], int(email_cfg.get("smtp_port", 587)), timeout=30) as server:
            server.starttls()
            server.login(email_cfg["username"], email_cfg["password"])
            server.send_message(msg)
        return True
    except Exception as exc:
        errors.append(f"email: {exc}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Fetch, dedupe and score new job postings.")
    parser.add_argument("--quiet", action="store_true", help="no browser; for scheduled runs")
    args = parser.parse_args()

    cfg = load_config()
    errors: list[str] = []
    all_jobs = (
        fetch_remotive(cfg, errors)
        + fetch_remoteok(cfg, errors)
        + fetch_wwr(cfg, errors)
        + fetch_adzuna(cfg, errors)
        + fetch_reed(cfg, errors)
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.get("days_back", 7))
    kept, kept_urls = [], set()
    for job in all_jobs:
        if job["posted"] and job["posted"].tzinfo and job["posted"] < cutoff:
            continue
        if job["url"] in kept_urls:  # same posting via several search queries
            continue
        if eligible(job, cfg):
            kept.append(job)
            kept_urls.add(job["url"])

    counts: dict[str, int] = {}
    for job in kept:
        counts[job["source"]] = counts.get(job["source"], 0) + 1

    fresh = split_new(kept, HERE / "seen.db")
    scored = []
    for job in fresh:
        points, matched = score(job, cfg)
        if points >= cfg.get("min_score", 10):
            scored.append((job, points, matched))
    scored.sort(key=lambda row: row[1], reverse=True)
    unique, seen_keys = [], set()
    for row in scored:  # same role posted under several categories/sources
        key = (row[0]["title"].lower(), row[0]["company"].lower())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique.append(row)
    scored = unique[: cfg.get("max_items", 30)]
    record_shown(scored, HERE / "seen.db")

    body = build_html(scored, errors, counts, cfg)
    out_dir = HERE / "digests"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    out_file = out_dir / f"digest-{stamp}.html"
    out_file.write_text(body, encoding="utf-8")
    (out_dir / "latest.html").write_text(body, encoding="utf-8")

    subject = f"Job digest: {len(scored)} new ({stamp})"
    emailed = send_email(cfg, subject, body, errors) if scored else False

    print(f"fetched={len(all_jobs)} eligible={len(kept)} new={len(fresh)} shown={len(scored)} emailed={emailed}")
    for job, points, _ in scored[:5]:
        print(f"  [{points:>3}] {job['title']} - {job['company']} ({job['source']})")
    if errors:
        print("errors:", "; ".join(errors))
    if not args.quiet:
        webbrowser.open(out_file.as_uri())


if __name__ == "__main__":
    main()
