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
| 11 | SerpApi key (free plan, 250/mo) | DONE | owner rotated key 2026-10-29 02:52 IST; probe run 40771506362 -> 37 fares; MAX_BOOKING_LOOKUPS=1 in workflow |
| 12 | GCP project | DONE | `flight-tracker-102924515` created; Gmail API enabled |
| 13 | Host: GitHub Actions workflow | DONE (code) | .github/workflows/tracker.yml; DB on `data` branch; tests test_actions_workflow_safe_and_hourly, test_require_trial_blocks_checks_before_start; 43 passed |
| 13b | Private repo + push + workflow smoke runs | DONE | https://github.com/priyanshu2007-cmd/flight-tracker (PRIVATE). Runs 40767445829 and 40767535758 (command=summary) succeeded: restore, run, save to `data` branch, stop-check |
| 13c | Repo secrets SERPAPI_API_KEY, GMAIL_TOKEN_JSON | DONE | `gh secret list` shows both (2026-10-29). Key rotated after chat exposure |
| 14 | Gmail OAuth client + consent | DONE | project flight-tracker-102924515, Desktop client, test user spec.priyanshu@gmail.com, token in secrets/gmail_token.json (0600, git-ignored) |
| 14b | Gmail token lifetime | DONE (decision) | Owner chose to keep Testing mode: token expires ~5 Nov 2026 02:35 IST. Publishing needs a public home + privacy URL on an owned domain (not created) |
| 14c | DB snapshots | DONE | workflow uploads tracker.db artifact per run, 30-day retention |
| 15 | Test email verified | DONE | `python -m tracker test-email` -> status delivered (message id 1a18455724f09d40), 2026-10-29 02:36 IST |
| 16 | Real fares from a permitted source | DONE | run 40770119464 (dry-run): 37 fares; lowest A 9,485 / B 9,529 / C 8,986 / D 8,286 INR; Google Flights = verified |
| 17 | Deploy + start 24 h trial | DONE | run 40770312379 (trial-start) succeeded; DB on data branch has trial row + 30 observations |

## Next recommended task
Watch hourly runs (`gh run list -w tracker.yml`). After the trial ends, read the summary email and decide whether to continue (`gh workflow enable tracker.yml`, then `trial-start`) Gmail token expires ~5 Nov 02:35 IST: re-run `python -m tracker auth` and `gh secret set GMAIL_TOKEN_JSON < secrets/gmail_token.json` to extend.

## Trial state
RUNNING. Started 2026-10-28T21:15:12Z (29 Oct 2026 02:45 IST). Ends 2026-10-29T21:15:12Z (30 Oct 2026 02:45 IST). Scheduled checks hourly at :17 via GitHub Actions; on the first run after the end time it emails the summary and disables the workflow.
