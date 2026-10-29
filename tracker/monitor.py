"""One monitoring run: fetch -> filter -> persist -> decide alerts -> send. Plus the 24h trial lifecycle."""
import fcntl
import json
import os
import time
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from . import db, mailer, sources
from .config import DRY_RUN, IST, TARGETS, THRESHOLD_INR
from .config import DB_PATH
from .core import Fare, decide, eligible

TIMER = "flight-tracker.timer"
BLOCK_HOURS = 24  # after a block/rate limit, leave that path alone this long (no retry, per Agent Reach docs)


@contextmanager
def run_lock(path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def process(con, run_id: int, fares: list) -> list[str]:
    """Persist eligible fares and queue alerts. Returns queued alert subjects."""
    targets = {t.id: t for t in TARGETS}
    fares = [f for f in fares if eligible(f, targets[f.target_id])]
    for f in fares:
        db.add_observation(con, run_id, f)
    # Same flight from several sources: keep the cheapest per kind; a verified deal outranks a lead.
    best: dict[tuple[str, str], Fare] = {}
    for f in fares:
        k = (f.key, f.alert_kind)
        if k not in best or f.alert_price < best[k].alert_price:
            best[k] = f
    queued = []
    for (key, kind), f in sorted(best.items()):
        if kind == "lead" and (key, "deal") in best:
            continue
        con.execute("BEGIN IMMEDIATE")
        try:
            fire, state = decide(db.get_state(con, key, kind), f.alert_price)
            if fire:
                hist = db.history(con, key, kind, run_id)
                subject, body = mailer.render(f, kind, hist)
                if mailer.queue(con, f"{key}|{kind}|{f.alert_price}|{run_id}", key, kind, f.alert_price,
                                hist.get("prev"), subject, body):
                    queued.append(subject)
            db.put_state(con, key, kind, state)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return queued


def _blocked(con, name: str) -> bool:
    r = con.execute("SELECT blocked_until FROM sources WHERE name=?", (name,)).fetchone()
    return bool(r and r[0] and datetime.fromisoformat(r[0]) > datetime.now(UTC))


def _health(con, name: str, status: str, err: str | None, got_fares: bool) -> None:
    mapped = {"ok": "verified", "no-results": "verified", "requires-authorization": "requires-authorization",
              "rate-limited": "temporarily-failed", "blocked": "blocked", "unsupported": "unsupported"}.get(
        status, "temporarily-failed")
    until = (datetime.now(UTC) + timedelta(hours=BLOCK_HOURS)).isoformat(timespec="seconds") \
        if status in ("blocked", "rate-limited", "unsupported") else None
    con.execute("UPDATE sources SET fare_tested=1, status=?, failure_reason=?, blocked_until=?,"
                " fares_usable=MAX(fares_usable, ?), last_success=CASE WHEN ? THEN ? ELSE last_success END WHERE name=?",
                (mapped, err, until, int(got_fares), status in ("ok", "no-results"), db.now(), name))


def collect(con, run_id: int, serp=sources.serpapi_fetch, reach=sources.agent_reach_fetch) -> list:
    fares, serp_stop = [], None
    active = db.active_targets(con)
    for t in TARGETS:
        if t.id not in active:
            db.check(con, run_id, "-", t.id, "skipped", 0, "target paused or booked")
            continue
        api_ok = False
        if serp_stop is None:
            status, got, err = serp(t)
            db.check(con, run_id, "Google Flights/SerpApi", t.id, status, len(got), err)
            _health(con, "Google Flights", status, err, bool(got))
            fares += got
            api_ok = status in ("ok", "no-results")
            if status in ("rate-limited", "requires-authorization"):
                serp_stop = status
        for name, url in sources.agent_reach_urls(t).items():
            reg = sources.AR_GOOGLE if name == "Google Flights" else name  # own row: API health must not clear its block
            if (name == "Google Flights" and api_ok) or _blocked(con, reg):
                continue
            status, got, err = reach(url)
            db.check(con, run_id, f"{name}/AgentReach", t.id, status, len(got), err)
            _health(con, reg, status, err, bool(got))
            fares += got
    return fares


def run_once(path: str = DB_PATH, dry_run: bool = DRY_RUN, **kw) -> dict:
    with run_lock(os.path.join(os.path.dirname(path) or ".", "run.lock")) as got:
        if not got:
            return {"status": "skipped", "reason": "another run is in progress"}
        con = db.connect(path)
        sources.seed_registry(con)
        trial = con.execute("SELECT * FROM trial WHERE id=1").fetchone()
        if trial and trial["status"] == "running" and datetime.now(UTC) >= datetime.fromisoformat(trial["ends_at"]):
            return {"status": "trial-ended", "summary": finish_trial(con, dry_run)}
        if not trial and os.environ.get("REQUIRE_TRIAL") == "1":
            return {"status": "skipped", "reason": "no trial started (run trial-start)"}
        if trial and trial["status"] == "completed" and os.environ.get("CONTINUE_AFTER_TRIAL") != "1":
            return {"status": "skipped", "reason": "trial completed; checks stopped"}
        run_id = con.execute("INSERT INTO runs (started_at, status) VALUES (?, 'running')", (db.now(),)).lastrowid
        try:
            fares = collect(con, run_id, **kw)
            queued = process(con, run_id, fares)
            sent = mailer.flush(con, dry_run=dry_run)
            con.execute("UPDATE runs SET finished_at=?, status='ok', note=? WHERE id=?",
                        (db.now(), f"{len(fares)} fares, {len(queued)} alerts", run_id))
            return {"status": "ok", "run": run_id, "fares": len(fares), "queued": queued, "sent": sent}
        except Exception as e:
            con.execute("UPDATE runs SET finished_at=?, status='failed', note=? WHERE id=?",
                        (db.now(), f"{type(e).__name__}: {str(e)[:200]}", run_id))
            raise


def start_trial(con, hours: int = 24, systemd: bool = False) -> dict:
    if systemd:
        subprocess.run(["systemctl", "--user", "enable", "--now", TIMER], check=True, timeout=30)
    start = datetime.now(UTC)
    con.execute("INSERT OR REPLACE INTO trial (id, started_at, ends_at, status) VALUES (1, ?, ?, 'running')",
                (start.isoformat(timespec="seconds"), (start + timedelta(hours=hours)).isoformat(timespec="seconds")))
    return dict(con.execute("SELECT * FROM trial WHERE id=1").fetchone())


def summary(con) -> str:
    out = []
    tr = con.execute("SELECT * FROM trial WHERE id=1").fetchone()
    if tr:
        out.append(f"Trial: {tr['status']}  {db.ist(tr['started_at'])} -> {db.ist(tr['ends_at'])}")
    runs = con.execute("SELECT status, COUNT(*) FROM runs GROUP BY status").fetchall()
    out.append("Runs: " + (", ".join(f"{r[0]}={r[1]}" for r in runs) or "none"))
    for t in TARGETS:
        r = con.execute("SELECT COUNT(*) n, COUNT(DISTINCT flight_key) k, MIN(CAST(displayed_inr AS REAL)) lo,"
                        " MIN(CAST(comparable_inr AS REAL)) clo FROM observations WHERE target_id=?", (t.id,)).fetchone()
        out.append(f"Target {t.id} {t.origin}->{t.dest} {t.date}{' after 17:00' if t.depart_after else ''}: "
                   f"{r['n']} observations of {r['k']} flights; lowest displayed {mailer.inr(r['lo'])}; "
                   f"lowest baggage-verified {mailer.inr(r['clo'])}")
    for s in con.execute("SELECT name, status, last_success, failure_reason FROM sources ORDER BY name"):
        out.append(f"Source {s['name']}: {s['status']} (last success {db.ist(s['last_success'])})"
                   f"{' - ' + s['failure_reason'] if s['failure_reason'] else ''}")
    al = con.execute("SELECT kind, status, COUNT(*) FROM alerts GROUP BY kind, status").fetchall()
    out.append("Alerts: " + (", ".join(f"{a[0]}/{a[1]}={a[2]}" for a in al) or "none"))
    out.append(f"Threshold: below {mailer.inr(THRESHOLD_INR)} per one-way ticket")
    return "\n".join(out)


STATUS_ANCHOR = datetime(2026, 10, 29, 8, 25, tzinfo=UTC)  # first status email: 13:55 IST
STATUS_EVERY = timedelta(hours=2)


def status_slot(now: datetime) -> datetime | None:
    """Most recent status-email slot at or before now (None before the first slot)."""
    k = (now - STATUS_ANCHOR) // STATUS_EVERY
    return STATUS_ANCHOR + k * STATUS_EVERY if k >= 0 else None


def status_email(con, dry_run: bool = False, now: datetime | None = None) -> dict:
    """Progress email for the current 2-hour slot, sent once per slot. Reads stored data only."""
    tr = con.execute("SELECT status FROM trial WHERE id=1").fetchone()
    slot = status_slot(now or datetime.now(UTC))
    if not tr or tr["status"] != "running" or slot is None:
        return {"status": "skipped", "reason": "no trial running or before first slot"}
    idem = f"status|{slot:%Y%m%dT%H%M}"
    if con.execute("SELECT 1 FROM alerts WHERE idem_key=?", (idem,)).fetchone():
        return {"status": "skipped", "reason": "slot already queued"}  # retries happen in run_once's flush
    body = summary(con) + "\n\nStatus update only: no fare is below the threshold unless an alert email says so."
    mailer.queue(con, idem, "-", "status", Decimal(0), None,
                 f"Flight tracker status: {slot.astimezone(IST):%d %b %H:%M} IST", body)
    return {"status": "ok", "sent": mailer.flush(con, dry_run=dry_run)}


def loop(path: str = DB_PATH, minutes: int = 330, dry_run: bool = DRY_RUN, tick: int = 60,
         sleep=time.sleep, clock=lambda: datetime.now(UTC), **kw) -> dict:
    """Stay alive up to `minutes`: hourly price check + 2-hourly status email, until the trial ends.

    Runs inside one GitHub Actions job (6 h limit); the workflow chains the next loop.
    """
    con = db.connect(path)
    end, last_check, checks = clock() + timedelta(minutes=minutes), None, 0
    while True:
        now = clock()
        tr = con.execute("SELECT status FROM trial WHERE id=1").fetchone()
        if not tr or tr["status"] != "running":
            return {"status": "stopped", "reason": "no running trial", "checks": checks}
        if last_check is None or now - last_check >= timedelta(hours=1):
            last_check = now
            try:
                r = run_once(path, dry_run=dry_run, **kw)
                print(json.dumps({"check": r.get("status"), "fares": r.get("fares"), "at": now.isoformat()}), flush=True)
                checks += 1
                if r.get("status") == "trial-ended":
                    return {"status": "trial-ended", "checks": checks}
            except Exception as e:  # keep looping; run_once already recorded the failed run
                print(json.dumps({"check": "failed", "error": f"{type(e).__name__}: {str(e)[:150]}"}), flush=True)
        st = status_email(con, dry_run, now)
        if st["status"] == "ok":
            print(json.dumps({"status_email": st["sent"]}), flush=True)
        if now + timedelta(seconds=tick) >= end:
            return {"status": "loop-complete", "checks": checks}
        sleep(tick)


def finish_trial(con, dry_run: bool = False) -> str:
    con.execute("UPDATE trial SET status='completed' WHERE id=1")
    text = summary(con)
    con.execute("UPDATE trial SET summary=? WHERE id=1", (text,))
    mailer.queue(con, "trial-summary|" + db.now()[:13], "-", "summary", Decimal(0), None,
                 "Flight tracker: 24-hour trial summary", text)
    mailer.flush(con, dry_run=dry_run)
    try:
        subprocess.run(["systemctl", "--user", "disable", "--now", TIMER], timeout=30, check=False,
                       capture_output=True)
    except (OSError, subprocess.TimeoutExpired):
        pass  # not on systemd (tests/mac): run_once already refuses to check after completion
    return text
