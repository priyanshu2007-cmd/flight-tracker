# Flight Price Tracker — Authoritative Specification

Owner-provided specification (2026-10-29). Condensed for the repo; every requirement is retained.

## Purpose
Reliable flight-price tracker using reputable, approved sources; suitable APIs/MCP first; Agent Reach for permitted
web research/reading when no API/MCP can supply the data. Persistent price history, ~hourly checks during an initial
24-hour trial, Gmail alerts on every genuine downward fare change below ₹7,500 per one-way ticket.
No WhatsApp. No automatic booking, reservation, checkout, payment or purchase. Owner buys manually.
Alert recipient: `spec.priyanshu@gmail.com`.

## 0. First-response gate
Skill-first preflight, read this spec and project instructions, ask the 3 questions in §1, wait. No implementation,
installs, file changes, external services, account authorization, scheduler or trial before answers. Afterwards work
independently. If an external authorization blocks progress, finish all independent work and name the exact action.
Never claim implementation, source verification, Gmail working or a running trial without testing it.

## 1. Clarification questions (answered — see TODO.md "Confirmed decisions")
1. Approved sources: official airline sites, Google Flights, Skyscanner, MakeMyTrip, ixigo, Cleartrip. Do not trust
   every aggregator seller; prefer official airline domains or approved platforms.
2. Gmail: confirm destination; ask whether it is also the sender. Official Gmail API + OAuth; owner authorizes.
   Never ask for passwords, tokens or app passwords in chat.
3. Baggage-unverified leads (one checked trolley in the hold + one cabin bag): separate, clearly labelled emails for
   promising sub-₹7,500 fares whose checked-bag price is unverified (recommended).

## 2. Skill-first preflight and token efficiency
Read installed Ponytail (FULL), Caveman, Token Efficiency instructions; read AGENTS.md, AI_TOOLKIT.md, CLAUDE.md;
check Git status. Reuse code, targeted reads, compact output, no needless subagents, deterministic code for dates,
fares, persistence, scheduling, dedup, email rules. Persist findings in docs and TODO.md. Never trade security,
source policy, tests or correctness for tokens. If a skill is missing: one discovery attempt, report, continue.

## 3. Targets (Asia/Kolkata for display; store UTC)
- A: BLR → LKO, 23 Dec 2026, departure strictly after 17:00 IST (reject ≤ 17:00).
- B: BLR → LKO, 24 Dec 2026, any time.
- C: LKO → BLR, 4 Jan 2027, any time.
- D: LKO → BLR, 5 Jan 2027, any time.
Independent one-way targets; never change years or broaden the 23 Dec window.
One adult; one checked trolley (hold) + one cabin bag — the trolley is not a cabin bag. Record allowances, weight,
piece limits, charges when available. Add checked-bag charge when verifiable. Never invent allowances or call an
incomplete fare all-inclusive. Prefer nonstop when comparable; allow connections if cheaper; show stops and duration.

## 4. Budget and alerts
INR. Strictly below ₹7,500 per one-way ticket (₹7,499 yes, ₹7,500 no). Never combine outbound + return.
Email when: (1) new eligible flight with verified comparable fare < ₹7,500; (2) observed flight gets cheaper while
below; (3) fare above drops below; (4) different eligible flight found below; (5) meaningful new low for flight/target.
No repeat emails for unchanged prices; increases are not drops; dedupe same flight + comparable fare across sources
while keeping each provider's observation; never suppress a genuine lower fare. Small configurable material-change
threshold, documented. Persist alert state. Track the lowest price observed by this service only.
States: `verified_total`, `verified_but_baggage_extra`, `baggage_cost_unknown`, `stale_observation`,
`discovery_only`, `availability_unknown`, `source_failed`. Unverified alerts must say the baggage-adjusted price is
not confirmed.

## 5. Source policy
Approved allowlist (confirmed). Verify airline domains. Never route to unknown sellers. Evaluate any new provider
(reputation, domain, docs, data quality, cost, limits, terms) before use; never silently expand the list.
Source registry: name/approval, domain, retrieval method, API/MCP availability, Agent Reach usage, authorization,
data fields, polling limits, actual test results, last success, health and failure reason. Statuses: `verified`,
`configured-but-not-verified`, `requires-authorization`, `temporarily-failed`, `blocked`, `unsupported`.
A homepage visit is not fare retrieval. Never fabricate fares, links, history, coverage or tests.

## 6. API/MCP first, Agent Reach when needed
Use https://github.com/public-apis/public-apis as a discovery catalogue; verify each candidate's official docs for
real fare search, BLR↔LKO date coverage, genuine comparable prices, baggage/fees, auth/pricing/quota/limits, terms,
approved-seller links. Investigate Amadeus first. Aviationstack is flight status, not shopping. Never invent endpoints.
Priority: official API → authorized MCP → Agent Reach (documented web search/read) → discovery-only if it returns
only a URL/snippet/page shell. Stay provider-independent.
Agent Reach: https://github.com/Panniantong/Agent-Reach (README_en, install.md, SKILL.md, capabilities.md,
maintenance.md). Check installed version first; follow official install only if needed; never install a same-named
unrelated package. Discover real commands; adapter uses argument arrays, timeouts, validation, error capture,
redaction, provenance. Never invent commands. Follow documented handling for failures, rate limits, auth and blocks;
no CAPTCHA/login bypass, no rotation of accounts/IPs/backends. A failed check is not proof of no flights.

## 7. Normalized fare model
Source + source flight ID, airline, flight number, route, date, departure/arrival, time zone, stops, duration, base
fare, taxes/fees, checked-bag charge, displayed total, baggage-adjusted total, currency, baggage rules + verification,
availability + fare verification, approved booking URL, observation/verification timestamps, provenance, freshness.
Unknown stays unknown. Normalize to INR, keeping original currency/amount/rate/time when converting. Dedupe without
discarding provider records; never merge different flights. Snippets, AI summaries, stale cache, marketing pages,
page shells and verification pages are not verified fares. Revalidate qualifying prices where permitted; never
automate checkout to discover fees.

## 8. Gmail-only notifications
Official Gmail API + OAuth; owner authorizes; no hard-coded credentials; env vars or secret manager; `.env.example`
placeholders. Implement: manual test email, dry-run, persistent records, idempotent events, duplicate suppression,
bounded retries, redacted logs, safe retry of undelivered events, accurate delivery states. After authorization send
one test email and verify before the trial.
Subject e.g. `Flight Price Drop: BLR → LKO | ₹7,200 | 24 Dec 2026`. Body: route/date, airline + flight number,
departure/arrival, stops/duration, current and previous fares, lowest observed + time, threshold reason, baggage
charges + verification status, approved source, latest verification time, direct booking URL, price-change warning.
Label incomplete fares. Accepted ≠ delivered.

## 9. 24-hour trial
24 h from actual successful scheduler start. Prerequisites: answers given, valid config, at least one permitted source
returns usable fares, storage works, Gmail authorized + test email succeeded, scheduler running/verified. Initial run
then ~hourly within provider limits; record all valid observations including > ₹7,500; no overlapping jobs; restart
recovery; per-source success/failure tracking. At 24 h stop automatically, keep history, write a summary, leave the
app available for manual reactivation. Never claim the trial is live when it is not.

## 10. Persistence and operations
SQLite acceptable single-instance (PostgreSQL for cloud scale). Persist targets + rules, observations/history,
source config/coverage/health, scheduler history, price changes, alerts + delivery, trial state, pause/booked state.
Timeouts, bounded retries, manual single run, dry-run, pause/resume, safe shutdown. Dashboard optional.

## 11. Tests (deterministic, mocked, dry-run e2e)
1 Dec 23 ≤ 17:00 rejected · 2 Dec 23 > 17:00 eligible · 3 Dec 24 any time · 4 Jan 4/5 independent · 5 ₹7,499 yes /
₹7,500 no · 6 per-ticket threshold · 7 first qualifying email · 8 new lower email · 9 unchanged silent · 10 increase
silent · 11 cross-source dedupe · 12 baggage handling · 13 history survives restart · 14 Agent Reach real interface ·
15 rate limits/blocks respected · 16 approved sources only · 17 failure ≠ empty search · 18 Gmail failure retried ·
19 dry-run sends nothing · 20 trial stops at 24 h and keeps history · 21 no secrets in logs/commits · 22 no WhatsApp/
booking/checkout/purchase path · 23 mock price drop simulation.

## 12–13. Workflow and cross-session persistence
Follow §12 order. Keep `CLAUDE.md` (resume rules) and `TODO.md` (checklist with NOT_STARTED/IN_PROGRESS/BLOCKED/DONE,
evidence, blockers + owner action, next task, confirmed decisions, trial state only if legitimately started).
Resume: read this file → CLAUDE.md → TODO.md → inspect workspace/Git → continue at first incomplete item.

## 14. Definition of done
Skills loaded or absence reported; questions answered first; Agent Reach integrated via its real interface; APIs/MCP
used when they supply data; sources approved and honestly labelled; at least one permitted source returned real fares;
target rules exact; history persists; every genuine drop below ₹7,500 emails, unchanged prices silent; Gmail test
verified before trial; 24 h trial genuinely runs and stops; tests run and reported; CLAUDE.md + TODO.md support
resumption; no WhatsApp or purchasing capability.
