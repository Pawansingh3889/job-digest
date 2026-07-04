# job-digest

A daily job digest and application pipeline that refuses to do the two things
every "auto apply" tool does: it does not scrape LinkedIn, and it does not
submit applications for you.

`digest.py` pulls new postings from official job APIs, remembers everything it
has already shown you in a local SQLite file, scores what is new against a
keyword profile you control, and writes an HTML digest (optionally emailed).
`apply.py` turns any digest item into an application pack: a snapshot of the
posting, a report of which requirements your CV already covers, and a screening
answers sheet built from facts you wrote down once. Then you tailor, you
submit, and the tool tracks the funnel.

Everything runs on your machine. No cloud database, no accounts, no telemetry.

## Why the submit click stays human

Bots that mass-apply get accounts restricted, waste recruiters' time, and
answer screening questions with canned defaults. Questions like "do you
require sponsorship" carry legal weight on an application form; a wrong
auto-answer is a false declaration under your name. This tool automates the
90% that is drudgery (finding, deduping, matching, remembering) and leaves the
10% that must be yours.

## Quickstart

Python 3.10+.

```
git clone https://github.com/Pawansingh3889/job-digest
cd job-digest
pip install -r requirements.txt
python digest.py
```

That fetches from the keyless sources, writes `digests/latest.html`, and opens
it. Run it again and you get only what is new since last time. For the apply
side:

```
copy profile.example.json profile.json    # then fill it in
python apply.py run           # refresh the digest right now, show the list
python apply.py run 3         # refresh, pack item 3, open the form, start fill
python apply.py list          # latest digest, numbered
python apply.py pick 3        # or: pick <url>  - builds applications/<slug>/
python apply.py fill 3        # walks the form: each field copied to the
                              # clipboard in turn, you paste, you submit
python apply.py applied 3     # after you submit; also: interview / rejected / offer
python apply.py status        # the whole funnel in one table
```

`fill` is the closest this tool gets to applying for you, on purpose. It works
on any application form because it never touches the page: open the form, run
`fill`, and paste your way down it. Fields still marked TODO in your profile
are called out before you start, so you cannot half-fill a real application by
accident.

## Sources

Working with no keys:

- Remotive (official API)
- Remote OK (official API)
- We Work Remotely (public RSS)

Those are global remote boards. For UK coverage, add free API keys and two
more sources switch on automatically:

- Adzuna: register at developer.adzuna.com
- Reed: register at reed.co.uk/developers

Keys go in `config.local.json` (gitignored), which overlays `config.json` key
by key:

```json
{
  "adzuna": { "app_id": "...", "app_key": "..." },
  "reed": { "api_key": "..." },
  "email": { "smtp_host": "smtp.gmail.com", "username": "...", "password": "app-password", "to": "you@example.com" }
}
```

## Scoring

Transparent keyword weighting, tuned in `config.json`: a `title_terms` hit is
+25, each `boost_terms` hit is +6, each `negative_terms` hit is -30, stated
salary is +5. Remote is a priority, not a gate: remote roles get
`remote_bonus` (+15), hybrid gets `hybrid_bonus` (+8), on-site still appears,
just lower. Every digest row shows the matched terms, so you can see why a
job scored what it did and adjust. Location filtering rejects postings
restricted to countries where you cannot be hired; the lists are yours to
edit.

## Scheduling

Windows, daily at 07:30 with catch-up after sleep:

```powershell
$a = New-ScheduledTaskAction -Execute "pythonw.exe" -Argument '"C:\path\to\digest.py" --quiet' -WorkingDirectory "C:\path\to\job-digest"
$t = New-ScheduledTaskTrigger -Daily -At 7:30am
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName "job-digest" -Action $a -Trigger $t -Settings $s
```

Linux or macOS, the cron line is:

```
30 7 * * * cd /path/to/job-digest && python3 digest.py --quiet
```

## Behind a corporate proxy or antivirus

If every HTTPS call fails with a certificate error, your machine has TLS
inspection. The tool uses `truststore` to read the OS certificate store, which
fixes that without disabling verification. It is installed by requirements.txt
on Python 3.10+ and skipped silently where not needed.

## Layout

```
digest.py        discovery: fetch, filter, score, dedupe, digest
apply.py         pipeline: pick, pack, track stages
config.json      queries, scoring terms, location rules (no secrets)
config.local.json  your keys and SMTP details (gitignored)
profile.json     your facts for answer sheets (gitignored; copy the example)
seen.db          local memory of every posting already shown (gitignored)
digests/         HTML output (gitignored)
applications/    one folder per application pack (gitignored)
```

## Live dashboard

```
python dashboard.py     # or: .\dashboard on Windows
```

Serves http://127.0.0.1:8765 (loopback only, standard library only) and
refreshes every three seconds from seen.db and applications/. Shows the
funnel as a board with per-pack readiness chips, the open-roles inbox, and a
computed "suggested next commands" panel: complete packs surface as fill
commands, unstaged picks as tailor commands, high-scoring unpicked roles as
pick commands, a stale digest as a refresh, and applications silent for two
weeks as follow-up reminders. Click a command to copy it.

## License

MIT
