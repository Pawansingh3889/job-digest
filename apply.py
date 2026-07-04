"""Application pipeline on top of the daily digest.

Automates everything up to the submit click, which stays human:

  apply.py run [n]              refresh the digest now; with n: pick, open
                                the form in the browser, start the fill walker
  apply.py list                 show the latest digest's roles, numbered
  apply.py pick <n | url>       build an application pack for a role
  apply.py fill <slug|n>        walk the form: copies each field to the
                                clipboard in order, you paste and submit
  apply.py applied <slug|n>     mark a pack as submitted
  apply.py interview <slug|n>   ... and the later stage transitions
  apply.py rejected <slug|n>
  apply.py offer <slug|n>
  apply.py status               the whole funnel in one table

A pack is a folder under applications/ holding the job snapshot, a keyword
match report against the base CV, and a screening answers sheet built from
profile.json. Nothing is ever submitted by this tool.
"""

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "seen.db"

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def db():
    con = sqlite3.connect(DB)
    con.execute(
        "CREATE TABLE IF NOT EXISTS apps (url TEXT PRIMARY KEY, slug TEXT UNIQUE, "
        "company TEXT, title TEXT, stage TEXT, created TEXT, updated TEXT)"
    )
    return con


def load(name, required=True):
    path = HERE / name
    if not path.exists():
        if required:
            hint = " (copy profile.example.json to profile.json and fill it in)" if name == "profile.json" else ""
            sys.exit(f"missing {name}{hint}")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def latest_shown(con):
    run = con.execute("SELECT MAX(run_date) FROM shown").fetchone()[0]
    if not run:
        return run, []
    rows = con.execute(
        "SELECT s.url, s.score, seen.title, seen.company, seen.payload "
        "FROM shown s JOIN seen ON seen.url = s.url "
        "WHERE s.run_date = ? ORDER BY s.score DESC",
        (run,),
    ).fetchall()
    return run, rows


def slugify(text, limit=60):
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:limit].rstrip("-") or "job"


def resolve_job(con, ref):
    """ref is a 1-based index into the latest digest, or a URL."""
    if ref.startswith("http"):
        row = con.execute(
            "SELECT url, NULL, title, company, payload FROM seen WHERE url = ?", (ref,)
        ).fetchone()
        if not row:
            sys.exit("that URL is not in seen.db; run digest.py first or check the link")
        return row
    run, rows = latest_shown(con)
    try:
        return rows[int(ref) - 1]
    except (ValueError, IndexError):
        sys.exit(f"no item {ref} in the latest digest ({run}); run: apply.py list")


def resolve_app(con, ref):
    if ref.isdigit():
        rows = con.execute("SELECT slug FROM apps ORDER BY created").fetchall()
        try:
            return rows[int(ref) - 1][0]
        except IndexError:
            sys.exit(f"no application #{ref}; run: apply.py status")
    row = con.execute("SELECT slug FROM apps WHERE slug LIKE ?", (f"%{ref}%",)).fetchone()
    if not row:
        sys.exit(f"no application matching '{ref}'")
    return row[0]


def detect_level(title):
    t = (title or "").lower()
    if re.search(r"\b(senior|sr|staff|principal|lead)\b", t):
        return "senior"
    if re.search(r"\b(junior|jr|graduate|grad|entry|trainee|apprentice)\b", t):
        return "junior"
    if re.search(r"\b(mid|intermediate)\b", t):
        return "mid"
    return None


def screening_answers_for(job, profile):
    """The stored answers, with the salary line swapped for the band matching
    the detected role level (junior / mid / senior / default)."""
    answers = dict(profile.get("screening_answers", {}))
    level = detect_level(job.get("title", ""))
    bands = profile.get("salary_bands", {})
    band = bands.get(level or "") or bands.get("default", "")
    if band and "TODO" not in band:
        for question in answers:
            if "salary" in question.lower():
                answers[question] = band
    return answers, level


def term_hits(terms, text):
    hits = []
    for term in terms:
        if re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", text):
            hits.append(term)
    return hits


def cmd_list():
    con = db()
    run, rows = latest_shown(con)
    if not rows:
        print("nothing recorded yet; run: python digest.py")
        return
    print(f"latest digest: {run}")
    for i, (url, score, title, company, _) in enumerate(rows, 1):
        print(f"  {i:>2}. [{score:>3}] {title} - {company}")
        print(f"      {url}")


def cmd_pick(ref):
    con = db()
    cfg = load("config.json")
    profile = load("profile.json")
    url, score, title, company, payload = resolve_job(con, ref)
    job = json.loads(payload) if payload else {"url": url, "title": title, "company": company, "description": "", "location": "", "salary": "", "source": "?"}

    slug = f"{datetime.now():%Y-%m-%d}-{slugify(company or 'unknown')}-{slugify(title, 40)}"
    folder = HERE / "applications" / slug
    folder.mkdir(parents=True, exist_ok=True)

    jd_text = (job["title"] + " " + job.get("description", "")).lower()

    # --- job.md: the snapshot ------------------------------------------------
    (folder / "job.md").write_text(
        f"# {job['title']}\n\n"
        f"- Company: {job['company']}\n"
        f"- Location: {job.get('location') or 'remote / unspecified'}\n"
        f"- Salary: {job.get('salary') or 'not stated'}\n"
        f"- Source: {job.get('source', '?')}"
        + (f" (score {score})" if score is not None else "") + "\n"
        f"- Link: {job['url']}\n"
        f"- Picked: {datetime.now():%Y-%m-%d %H:%M}\n\n"
        "## Description snapshot\n\n"
        + (job.get("description") or "(description was not captured; open the link)"),
        encoding="utf-8",
    )

    # --- match.md: JD vs base CV, mechanically -------------------------------
    lexicon = sorted(set(
        t.lower() for t in cfg.get("title_terms", []) + cfg.get("boost_terms", [])
        + profile.get("skills_lexicon", [])
    ))
    cv_text = ""
    cv_used = None
    for candidate in profile.get("base_cv_paths", []):
        path = Path(candidate)
        if path.exists():
            cv_text = path.read_text(encoding="utf-8", errors="replace").lower()
            cv_used = path
            break
    jd_terms = term_hits(lexicon, jd_text)
    covered = [t for t in jd_terms if t in cv_text] if cv_text else []
    missing = [t for t in jd_terms if t not in covered]
    demand_lines = [
        line.strip()[:200]
        for line in (job.get("description") or "").splitlines()
        if re.search(r"\b(essential|required|must have|you will need|we need)\b", line, re.I)
    ][:12]
    (folder / "match.md").write_text(
        f"# Match report: {job['title']} at {job['company']}\n\n"
        f"Base CV: {cv_used or 'NOT FOUND - set base_cv_paths in profile.json'}\n\n"
        "## JD terms already on the CV\n\n"
        + ("".join(f"- {t}\n" for t in covered) or "- none detected\n")
        + "\n## JD terms NOT on the CV (weave in if true, prep to discuss if not)\n\n"
        + ("".join(f"- {t}\n" for t in missing) or "- none detected\n")
        + "\n## Lines the JD marks as required\n\n"
        + ("".join(f"> {line}\n\n" for line in demand_lines) or "(none matched the required/essential patterns)\n"),
        encoding="utf-8",
    )

    # --- answers.md: screening sheet from stored truth ------------------------
    answers, level = screening_answers_for(job, profile)
    body = [
        f"# Screening answers: {job['company']}",
        "",
        f"Advertised salary: {job.get('salary') or 'not stated'} "
        "(sanity-check your expectation against this before pasting)",
        f"Detected level: {level or 'unspecified'} (salary answer selected accordingly)",
        "",
    ]
    for question, answer in answers.items():
        body.append(f"**{question}**")
        body.append(f"{answer}")
        body.append("")
    for note in profile.get("cautions", []):
        body.append(f"CAUTION: {note}")
    (folder / "answers.md").write_text("\n".join(body), encoding="utf-8")

    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT OR REPLACE INTO apps (url, slug, company, title, stage, created, updated) "
        "VALUES (?, ?, ?, ?, COALESCE((SELECT stage FROM apps WHERE url = ?), 'picked'), "
        "COALESCE((SELECT created FROM apps WHERE url = ?), ?), ?)",
        (job["url"], slug, job["company"], job["title"], job["url"], job["url"], now, now),
    )
    con.commit()
    print(f"pack ready: {folder}")
    print("  job.md, match.md, answers.md")
    print("next: tailor the CV from match.md, then submit yourself, then: "
          f"python apply.py applied {slug}")
    return slug, job["url"]


def set_stage(ref, stage):
    con = db()
    slug = resolve_app(con, ref)
    con.execute(
        "UPDATE apps SET stage = ?, updated = ? WHERE slug = ?",
        (stage, datetime.now(timezone.utc).isoformat(), slug),
    )
    con.commit()
    print(f"{slug} -> {stage}")


def to_clipboard(text):
    import subprocess
    try:
        if sys.platform == "win32":
            subprocess.run("clip", input=text.encode("utf-16-le"), check=True)
        elif sys.platform == "darwin":
            subprocess.run("pbcopy", input=text.encode(), check=True)
        else:
            subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=True)
        return True
    except Exception:
        return False


def cmd_fill(ref, print_only=False):
    con = db()
    profile = load("profile.json")
    slug = resolve_app(con, ref)
    row = con.execute("SELECT company, title, url FROM apps WHERE slug = ?", (slug,)).fetchone()
    company, title, url = row

    fields = []
    for key in ("name", "email", "phone", "location", "linkedin", "github", "portfolio"):
        if profile.get(key):
            fields.append((key, profile[key]))
    answers, level = screening_answers_for({"title": title}, profile)
    for question, answer in answers.items():
        fields.append((question, answer))

    todos = [label for label, value in fields if "TODO" in str(value)]
    print(f"filling: {title} at {company}")
    print(f"detected level: {level or 'unspecified'} (salary answer selected accordingly)")
    print(f"open the form: {url}\n")
    if todos:
        print("WARNING these fields still say TODO in profile.json:")
        for label in todos:
            print(f"  - {label}")
        print("fix them first or type the real answer on the form.\n")
    for caution in profile.get("cautions", []):
        print(f"CAUTION: {caution}\n")

    if print_only:
        for label, value in fields:
            print(f"{label}: {value}")
        return

    print("each field is copied to your clipboard in turn; paste it, then press Enter here.")
    print("(s + Enter skips a field, q + Enter quits)\n")
    for label, value in fields:
        copied = to_clipboard(str(value))
        state = "copied" if copied else "CLIPBOARD FAILED, copy manually"
        try:
            answer = input(f"  {label}: {value}   [{state} - Enter=next, s=skip, q=quit] ")
        except EOFError:
            break
        if answer.strip().lower() == "q":
            break
    print(f"\nwhen submitted: python apply.py applied {slug}")


def cmd_run(ref=None):
    import subprocess
    import webbrowser

    print("refreshing digest...")
    result = subprocess.run(
        [sys.executable, str(HERE / "digest.py"), "--quiet"],
        cwd=HERE, capture_output=True, text=True,
    )
    print(result.stdout.strip())
    if not ref:
        print()
        cmd_list()
        print("\nnext: python apply.py run <n>  (pack + open form + fill walker)")
        return
    slug, url = cmd_pick(ref)
    webbrowser.open(url)
    print()
    cmd_fill(slug)


def cmd_status():
    con = db()
    rows = con.execute(
        "SELECT slug, company, title, stage, substr(created, 1, 10), substr(updated, 1, 10) "
        "FROM apps ORDER BY created"
    ).fetchall()
    if not rows:
        print("no applications tracked yet; run: apply.py pick <n>")
        return
    print(f"{'#':>2}  {'stage':<10} {'company':<24} {'title':<38} {'picked':<11} updated")
    for i, (slug, company, title, stage, created, updated) in enumerate(rows, 1):
        print(f"{i:>2}  {stage:<10} {(company or '')[:23]:<24} {(title or '')[:37]:<38} {created:<11} {updated}")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd, rest = args[0], args[1:]
    if cmd == "run":
        cmd_run(rest[0] if rest else None)
    elif cmd == "list":
        cmd_list()
    elif cmd == "pick" and rest:
        cmd_pick(rest[0])
    elif cmd == "fill" and rest:
        cmd_fill(rest[0], print_only="--print" in rest)
    elif cmd in ("applied", "interview", "rejected", "offer") and rest:
        set_stage(rest[0], cmd)
    elif cmd == "status":
        cmd_status()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
