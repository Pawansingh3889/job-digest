# LinkedIn, end to end (without scraping or auto-submit)

LinkedIn has no jobs API, and every "LinkedIn scraper" risks your account.
Auto-submitting is worse: LinkedIn Easy Apply asks screening questions
(sponsorship, right to work) that carry legal weight, and a canned answer is a
false declaration under your name. So this pipeline automates everything except
the two steps that must stay human, and uses the channel LinkedIn actually
offers: Job Alerts by email.

## The flow

1. LinkedIn emails you matching jobs (you set the alerts, once).
2. `python mailwatch.py --harvest` reads those emails and seeds the roles into
   your digest inbox, next to the board jobs.
3. `python apply.py list` / the dashboard show them. Pick the ones worth it.
4. LinkedIn job pages need a login, so a fresh pack has no JD. Paste the
   description into the pack's `job.md`, or read it in a Claude session with the
   browser extension, then `apply.py tailor`.
5. `apply.py fill` walks the form. You submit. `apply.py applied`.

## Setup (two minutes, once)

- On linkedin.com/jobs, run your searches (e.g. "data engineer, United Kingdom,
  Remote"), then toggle **Set alert** on each. Choose daily email.
- Put a Gmail app password in `config.local.json` (same block SMTP uses), so
  the harvester can read your inbox read-only.

Then `mailwatch --harvest` daily (or add it to the scheduled task) turns
LinkedIn's own alerts into packed, trackable applications, with the submit
click yours and every screening answer true.
