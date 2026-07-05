"""Check companies against the UK register of licensed sponsors (Workers).

The register is the official gov.uk CSV of every employer licensed to sponsor
a Skilled Worker visa. If you need sponsorship, a company that is not on it
cannot hire you, however good the fit. This module keeps a cached copy and
answers "is this company on it?".

Caveat that stays true: companies sometimes appear under a different legal
entity name, so "no match" means "check by hand before writing off", not
"definitely cannot sponsor". A match is solid.
"""

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CSV_PATH = HERE / "sponsors.csv"
META_PATH = HERE / "sponsors.meta.json"
PUBLICATION = "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"
UA = {"User-Agent": "job-digest/0.1 (personal job search tool)"}

GENERIC = {
    "ltd", "limited", "plc", "llp", "lp", "uk", "gb", "group", "holdings",
    "the", "co", "company", "inc", "gmbh", "llc", "technologies", "technology",
    "software", "services", "solutions", "consulting", "recruitment",
}

_names_blob = None  # lowercase register names, newline-joined, loaded once


def ensure_register(max_age_days=7, errors=None):
    """Download the current register CSV if the cache is stale. Returns True
    when a usable register file exists."""
    if CSV_PATH.exists() and META_PATH.exists():
        try:
            fetched = datetime.fromisoformat(json.loads(META_PATH.read_text())["fetched"])
            if datetime.now(timezone.utc) - fetched < timedelta(days=max_age_days):
                return True
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    try:
        page = requests.get(PUBLICATION, headers=UA, timeout=30)
        page.raise_for_status()
        match = re.search(r'https://assets\.publishing\.service\.gov\.uk/media/[a-z0-9]+/[^"]*?\.csv', page.text)
        if not match:
            raise RuntimeError("no CSV link found on the publication page")
        csv_resp = requests.get(match.group(0), headers=UA, timeout=120)
        csv_resp.raise_for_status()
        CSV_PATH.write_bytes(csv_resp.content)
        META_PATH.write_text(json.dumps({
            "fetched": datetime.now(timezone.utc).isoformat(),
            "source": match.group(0),
        }))
        return True
    except Exception as exc:
        if errors is not None:
            errors.append(f"sponsor register refresh failed: {str(exc)[:80]}")
        return CSV_PATH.exists()  # stale cache still beats nothing


def _load_blob():
    global _names_blob
    if _names_blob is None:
        import csv as csvmod
        with open(CSV_PATH, encoding="utf-8", errors="replace", newline="") as f:
            reader = csvmod.reader(f)
            next(reader, None)
            _names_blob = "\n".join(row[0].strip().lower() for row in reader if row and row[0].strip())
    return _names_blob


def significant_tokens(company):
    words = re.findall(r"[a-z0-9]+", (company or "").lower())
    sig = [w for w in words if w not in GENERIC]
    return sig or words


def check(company):
    """Returns (status, detail): ('sponsor', matched legal name) or
    ('no-match', '') or ('unknown', reason)."""
    if not company or not company.strip():
        return "unknown", "no company name"
    if not CSV_PATH.exists():
        return "unknown", "register not downloaded"
    tokens = significant_tokens(company)
    if not tokens:
        return "unknown", "no usable name tokens"
    blob = _load_blob()
    pattern = re.compile(
        r"^.*\b" + r"\W+".join(re.escape(t) for t in tokens) + r"\b.*$", re.M
    )
    match = pattern.search(blob)
    if match:
        return "sponsor", match.group(0).strip()[:60]
    return "no-match", ""


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    errors = []
    ok = ensure_register(errors=errors)
    print("register ready:", ok, errors or "")
    for name in sys.argv[1:] or ["Monzo", "GitLab", "GoCardless", "Sustainable Energy First"]:
        status, detail = check(name)
        print(f"  {name:30} {status:9} {detail}")
