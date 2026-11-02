"""Daily campaign until 30 Nov 2026: two fare checks a day and one daily email with that day's lowest fares.

`tick()` is idempotent and catches up: the workflow calls it hourly, and any tick after a slot's time does
that slot once. A missed day's email is sent late, labelled, so there are no gaps in the daily series.
"""
import os
from datetime import UTC, date, datetime, time, timedelta

from . import db, mailer, monitor, sources
from .config import DB_PATH, DRY_RUN, IST, TARGETS
from .core import approved_url

START, END = date(2026, 11, 2), date(2026, 11, 30)  # inclusive, IST
CHECKS = {"check_am": time(8, 0), "check_pm": time(20, 0)}  # IST; two looks a day, 12 h apart
EMAIL_AT = time(20, 30)  # daily email after the evening check
SEARCHES_PER_CHECK = 4  # one SerpApi search per target (booking lookups only for sub-threshold fares)

SCHEMA = "CREATE TABLE IF NOT EXISTS daily_jobs (day TEXT, kind TEXT, done_at TEXT, note TEXT, PRIMARY KEY (day, kind));"


def _done(con, day: date, kind: str) -> bool:
    return con.execute("SELECT 1 FROM daily_jobs WHERE day=? AND kind=?", (day.isoformat(), kind)).fetchone() is not None


def _mark(con, day: date, kind: str, note: str = "") -> None:
    con.execute("INSERT OR REPLACE INTO daily_jobs VALUES (?,?,?,?)", (day.isoformat(), kind, db.now(), note))


def _budget_allows_am(today: date, left: int | None) -> bool:
    """Skip the morning check when the SerpApi quota cannot also cover every remaining evening check."""
    if left is None:
        return True
    days_left = (END - today).days + 1
    return left >= days_left * SEARCHES_PER_CHECK + SEARCHES_PER_CHECK


def day_rows(con, t, day: date):
    lo = datetime.combine(day, time.min, IST).astimezone(UTC).isoformat()
    hi = datetime.combine(day + timedelta(days=1), time.min, IST).astimezone(UTC).isoformat()
    return con.execute("SELECT * FROM observations WHERE target_id=? AND observed_at>=? AND observed_at<?"
                       " ORDER BY CAST(displayed_inr AS REAL), id DESC", (t.id, lo, hi)).fetchall()


def _lowest_ever(con, t):
    return con.execute("SELECT displayed_inr, observed_at FROM observations WHERE target_id=?"
                       " ORDER BY CAST(displayed_inr AS REAL), id LIMIT 1", (t.id,)).fetchone()


def email_body(con, day: date) -> tuple[str, str]:
    parts, short = [], []
    for t in TARGETS:
        rows = day_rows(con, t, day)
        head = f"Target {t.id} {t.origin}->{t.dest} {t.date:%d %b %Y}{' after 17:00' if t.depart_after else ''}"
        if not rows:
            parts.append(f"{head}: no fresh fares today (source failed or quota used up)")
            short.append(f"{t.id} n/a")
            continue
        o = rows[0]
        price = int(float(o["displayed_inr"]))
        dep = datetime.fromisoformat(o["depart"]).astimezone(IST)
        stops = "nonstop" if o["stops"] == 0 else f"{o['stops']} stop"
        prev = day_rows(con, t, day - timedelta(days=1))
        change = ""
        if prev:
            d = price - int(float(prev[0]["displayed_inr"]))
            change = " (same as yesterday)" if d == 0 else f" ({'down' if d < 0 else 'up'} ₹{abs(d):,} vs yesterday)"
        ever = _lowest_ever(con, t)
        seen = datetime.fromisoformat(o["observed_at"]).astimezone(IST)
        lines = [f"{head}: today's lowest ₹{price:,}{change}",
                 f"  {o['airline']} {o['flight_numbers']}, departs {dep:%d %b %H:%M} IST, {stops}; seen {seen:%H:%M} IST on {o['seller'] or o['source']}",
                 f"  Lowest seen since tracking began: ₹{int(float(ever[0])):,} ({datetime.fromisoformat(ever[1]).astimezone(IST):%d %b})"]
        if approved_url(o["booking_url"]):
            lines.append(f"  Link: {o['booking_url']}")
        parts.append("\n".join(lines))
        short.append(f"{t.id} ₹{price:,}")
    subject = f"Flight prices {day:%d %b}: " + " · ".join(short)
    note = ("\n\nAlerts below ₹7,500 are emailed separately as soon as a check sees them. "
            "Prices exclude checked baggage unless an alert says otherwise; confirm on the booking page.")
    return subject, "\n\n".join(parts) + note


def tick(path: str = DB_PATH, dry_run: bool = DRY_RUN, now: datetime | None = None, quota=None, **kw) -> dict:
    now = (now or datetime.now(UTC)).astimezone(IST)
    today = now.date()
    if today > END:
        return {"status": "campaign-ended"}
    con = db.connect(path)
    con.executescript(SCHEMA)
    did = []
    if today >= START:
        for kind, at in CHECKS.items():
            if now.time() < at or _done(con, today, kind):
                continue
            if kind == "check_am" and now.time() >= CHECKS["check_pm"]:
                _mark(con, today, kind, "superseded by evening check")  # catch-up after 20:00: one check, not two
                continue
            left = quota() if quota else sources.quota_left(os.environ.get("SERPAPI_API_KEY", ""))
            if kind == "check_am" and not _budget_allows_am(today, left):
                _mark(con, today, kind, f"skipped to save quota ({left} left)")
                did.append(f"{kind}: skipped (quota {left})")
                continue
            r = monitor.run_once(path, dry_run=dry_run, ignore_trial=True, **kw)
            if r.get("status") == "ok":
                _mark(con, today, kind, f"{r['fares']} fares")
            did.append(f"{kind}: {r.get('status')} {r.get('fares', '')}".strip())
    # daily emails: today after EMAIL_AT (once the evening check has run or 23:00 passed), plus any missed past day
    day = START
    while day <= today:
        due = day < today or (now.time() >= EMAIL_AT and (_done(con, day, "check_pm") or now.time() >= time(23, 0)))
        if due and not _done(con, day, "email"):
            subject, body = email_body(con, day)
            if day < today:
                subject = "[late] " + subject
            mailer.queue(con, f"daily|{day.isoformat()}", "-", "daily", 0, None, subject, body)
            _mark(con, day, "email")
            did.append(f"email {day}")
        day += timedelta(days=1)
    sent = mailer.flush(con, dry_run=dry_run)  # also retries any earlier undelivered email
    return {"status": "ok", "did": did, "sent": sent}
