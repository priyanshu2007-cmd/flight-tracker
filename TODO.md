# TODO — flight tracker

## Confirmed decisions (2026-10-29) — do not re-ask
- Sources approved: official airline sites, Google Flights, Skyscanner, MakeMyTrip, ixigo, Cleartrip.
- Gmail: spec.priyanshu@gmail.com is sender and recipient. Gmail API OAuth; the owner clicks Allow. The owner asked
  for everything else to be automated.
- Baggage-unverified fares: send separate, clearly labelled UNVERIFIED LEAD emails.
- Hosting: GitHub Actions hourly cron (owner chose this 2026-10-29 after GCP billing was found closed).
- Flight Finder (affromero/flight-finder @636111d) rejected: stealth Playwright (webdriver hiding, random UA), VPN IP rotation, and LLM price extraction all break the spec.

## Checklist
| # | Item | Status | Evidence / blocker |
|---|------|--------|--------------------|
| 1 | Skill preflight (Ponytail full, Caveman, Token Efficiency) | DONE | Read in-session; no AGENTS.md / AI_TOOLKIT.md in repo; workspace was empty (no git, no WhatsApp code) |
| 2 | Agent Reach inspected | DONE | v1.5.0 at ~/.local/bin; no `read`/`channels` commands; web channel = `curl https://r.jina.ai/URL` (skill references/web.md); ENVIRONMENT_RISK rule = stop, no retry |
| 3 | API/MCP evaluation | DONE | public-apis: Amadeus (Self-Service shut down 17 Jul 2026 per multiple reports), Aviationstack (status only), others not fare APIs. SerpApi google_flights = only structured fare API found (free 250/mo, $25 1k, $75 5k) |
| 4 | Source probes | DONE | Jina: Google 403 AbuseAlleviation; ixigo "too many requests"; MMT/Skyscanner empty; Cleartrip shell without fares. robots.txt disallows ixigo /search/result/, Cleartrip /flights/search*, Skyscanner /transport/*. Direct Google fetch with a browser UA was refused (bot-check circumvention) and is not used |
| 5 | Fare model, rules, states, dedupe, alert engine | DONE | tracker/core.py, tracker/monitor.py |
| 6 | Persistence (SQLite, UTC timestamps, restart-safe) | DONE | tracker/db.py; test_history_and_state_survive_restart |
| 7 | Gmail sender, outbox, retries, dry-run, test command | DONE (code) | tracker/mailer.py; live send not yet possible (see 11) |
| 8 | Scheduler + 24 h trial lifecycle + pause/booked | DONE (code) | deploy/flight-tracker.{service,timer}; monitor.start_trial/finish_trial |
| 9 | Tests | DONE | `uv run pytest -q` → 43 passed (2026-10-29) |
| 10 | Live dry-run from Mac | DONE | `python -m tracker check --dry-run` → run ok, 0 fares; statuses recorded: SerpApi requires-authorization, Google/Jina blocked 403, MMT/ixigo/Skyscanner robots-blocked, Cleartrip unsupported |
| 11 | Owner: SerpApi key | BLOCKED | Owner signs up at serpapi.com and puts `SERPAPI_API_KEY=...` in `.env` (not chat). Trial needs ~100–400 searches → Starter ($25) recommended; free 250 works with `MAX_BOOKING_LOOKUPS=1` |
| 12 | GCP project | DONE | `flight-tracker-102924515` created; Gmail API enabled |
| 13 | Host: GitHub Actions workflow | DONE (code) | .github/workflows/tracker.yml; DB on `data` branch; tests test_actions_workflow_safe_and_hourly, test_require_trial_blocks_checks_before_start; 43 passed |
| 13b | Owner: `gh auth login`, then create private repo + push + secrets | BLOCKED | gh not logged in on this Mac |
| 14 | Gmail OAuth client + consent | BLOCKED | Needs the owner signed in to Cloud Console (create Desktop OAuth client, download JSON to secrets/gmail_client.json) and then run `uv run python -m tracker auth` and click Allow |
| 15 | Test email verified | NOT_STARTED | after 14 |
| 16 | At least one source returns real fares | NOT_STARTED | after 11 |
| 17 | Deploy + start 24 h trial | NOT_STARTED | after 11, 13, 14, 15, 16 |

## Next recommended task
Owner: `gh auth login`, SerpApi key, Gmail OAuth client + `tracker auth`. Then follow README "Deploy" (repo, secrets, test-email, trial-start).

## Trial state
Not started.
