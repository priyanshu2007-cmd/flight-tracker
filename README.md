# Flight price tracker

Monitors four one-way targets (BLR→LKO 23 Dec 2026 after 17:00 IST, BLR→LKO 24 Dec 2026, LKO→BLR 4 Jan 2027,
LKO→BLR 5 Jan 2027) and emails `spec.priyanshu@gmail.com` on every genuine drop below ₹7,500 per ticket.
It never books or pays. Spec: `FLIGHT_TRACKER_PROMPT.md`. Status and blockers: `TODO.md`.

## How it works
- `tracker/sources.py` — SerpApi Google Flights API first (search + booking options for sub-₹7,500 fares, giving
  seller price and baggage text). Agent Reach web reader (`curl https://r.jina.ai/URL`, its documented interface)
  is the fallback for the other approved sites. It checks robots.txt first, and a block, rate limit or page with no
  fares pauses that path for 24 h with no retry.
- `tracker/core.py` — eligibility, baggage parsing, verification state, alert decision.
- `tracker/monitor.py` — one run (file lock prevents overlap), persistence, dedupe, trial lifecycle.
- `tracker/mailer.py` — Gmail API (scopes `gmail.send` + `gmail.metadata`), persistent outbox, retries.
  `delivered` = message reached INBOX (self-addressed), `accepted` = Gmail took it but INBOX not confirmed.

## Alert rules (defaults)
- Price must be `< 7500` (strict), per one-way ticket and per target.
- **Deal** email: seller price fetched this run and checked + cabin baggage verified. Price = fare + checked-bag fee.
- **UNVERIFIED LEAD** email: price shown < ₹7,500 but baggage or seller price is unconfirmed. Separate state.
- Emails fire on the first qualifying price, on every new lower price (`MIN_DROP_INR`, default ₹1), and when a fare
  falls back below the threshold after rising above it. Unchanged prices and increases stay silent.
- The same flight from two sources gets one email, and both observations are kept.

## Commands
```
uv sync
uv run pytest -q                                   # test suite
uv run python -m tracker check --dry-run           # one pass, no email
uv run python -m tracker simulate-drop [--dry-run] # 7,400 -> 7,200 simulated deal, emails twice
uv run python -m tracker auth                      # owner Gmail consent (browser)
uv run python -m tracker test-email
uv run python -m tracker trial-start [--systemd]   # 24 h window + initial run
uv run python -m tracker pause|resume|booked A|B|C|D|all
uv run python -m tracker summary | sources | flush
```

## Secrets
Copy `.env.example` to `.env`. Put `SERPAPI_API_KEY` there by editing the file yourself, never in chat.
`secrets/gmail_client.json` is the OAuth client you download. `secrets/gmail_token.json` is written by `auth`
with mode 0600. `.env`, `secrets/` and `data/` are git-ignored.

## Gmail setup (owner, one time, project `flight-tracker-102924515`)
1. https://console.cloud.google.com/auth/branding?project=flight-tracker-102924515: app name "flight-tracker",
   support + contact email spec.priyanshu@gmail.com, audience **External**.
2. https://console.cloud.google.com/auth/audience?project=flight-tracker-102924515: under Test users, add spec.priyanshu@gmail.com.
3. https://console.cloud.google.com/auth/clients?project=flight-tracker-102924515: Create client, type **Desktop app**,
   then download the JSON and save it as `secrets/gmail_client.json`.
4. `uv run python -m tracker auth`. The browser shows "Google hasn't verified this app": click Continue and Allow.

## Deploy (GitHub Actions — chosen host)
`.github/workflows/tracker.yml` runs hourly at :17 (GitHub may delay scheduled runs). One run at a time
(`concurrency`). The SQLite DB lives on the `data` branch: each run restores it, runs, checkpoints the WAL and
force-pushes a one-commit `data` branch. The DB itself holds the full history, and the repo must be **private**.
`REQUIRE_TRIAL=1` makes scheduled checks do nothing until you start the trial.

One-time setup (owner):
```
gh auth login
gh repo create flight-tracker --private --source . --push
gh secret set SERPAPI_API_KEY            # paste the key at the hidden prompt, not in chat
gh secret set GMAIL_TOKEN_JSON < secrets/gmail_token.json   # after `python -m tracker auth`
gh workflow run tracker.yml -f command=test-email           # check the inbox
gh workflow run tracker.yml -f command=trial-start          # 24 h window + first check
```
When the 24 h are up, the next run emails the summary and the workflow disables itself. To reactivate, run
`gh workflow enable tracker.yml` and then `-f command=trial-start` again.
If the OAuth consent screen is in "Testing" mode, Google expires the refresh token after 7 days. That is fine for
the trial; publish the app if you want monitoring to continue past that.

Alternative host: `deploy/flight-tracker.{service,timer}` (systemd user timer on any Linux VM, `trial-start --systemd`).

## Backup / restore
Actions: every run's DB is on the `data` branch (`git show origin/data:tracker.db > tracker.db`). VM:
`deploy/backup.sh gs://BUCKET` (cron nightly) runs `sqlite3 .backup` and uploads the copy. To restore, stop the
timer, copy the backup over `data/tracker.db` and start the timer again. The schema is created idempotently on
every start.

## Source findings (2026-10-29, evidence in TODO.md)
| Source | Method | Result |
|---|---|---|
| Google Flights | SerpApi API | requires-authorization (needs owner's API key) |
| Google Flights | Agent Reach (Jina) | blocked: Jina anonymous access to google.com is banned (HTTP 403) |
| MakeMyTrip | Agent Reach | blocked by robots.txt / no fare data |
| ixigo | Agent Reach | robots.txt disallows `/search/result/`, and Jina got "too many requests" |
| Cleartrip | Agent Reach | page read; no structured fares (JS-rendered) → unsupported |
| Skyscanner | Agent Reach | robots.txt disallows `/transport/*`, and its API is partner-only |
| Amadeus Self-Service | API | shut down 17 Jul 2026 per multiple reports → unsupported |
| Aviationstack | API | flight status only, not fares → unsupported |
