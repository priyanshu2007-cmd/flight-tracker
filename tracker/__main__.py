"""CLI: python -m tracker <command>. No command books, buys or checks out anything."""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal

from . import db, mailer, monitor
from .config import DB_PATH, DRY_RUN, IST, TARGETS
from .core import Fare


def _con():
    from .sources import seed_registry
    con = db.connect(DB_PATH)
    seed_registry(con)
    return con


def cmd_auth(_):
    """Owner-only OAuth consent in a browser; stores the refresh token with 0600 permissions."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    creds = InstalledAppFlow.from_client_secrets_file(mailer.CLIENT_FILE, mailer.SCOPES).run_local_server(port=0)
    os.makedirs(os.path.dirname(mailer.TOKEN_FILE) or ".", exist_ok=True)
    fd = os.open(mailer.TOKEN_FILE, os.O_WRONLY | os.O_TRUNC | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(creds.to_json())
    print(f"token saved to {mailer.TOKEN_FILE}")


def cmd_test_email(a):
    subject = "Flight tracker: test email"
    body = f"Test sent {datetime.now(IST):%d %b %Y %H:%M} IST. Gmail delivery for price alerts is working."
    if a.dry_run:
        print(json.dumps({"status": "dry-run", "subject": subject}))
        return
    res = mailer.GmailSender().send(subject, body)
    print(json.dumps(res))
    sys.exit(0 if res["status"] in ("accepted", "delivered") else 1)


def cmd_simulate_drop(a):
    """Feed a verified 7,400 -> 7,200 drop for target B through the real pipeline, in a throwaway DB."""
    con = db.connect(":memory:")
    t = TARGETS[1]
    depart = datetime.combine(t.date, datetime.min.time(), IST) + timedelta(hours=9, minutes=5)
    base = dict(target_id=t.id, source="SIMULATION", source_flight_id="sim", flight_numbers=("6E 0000",),
                airline="SIMULATION Air", origin=t.origin, dest=t.dest, depart=depart,
                arrive=depart + timedelta(hours=2, minutes=40), stops=0, duration_min=160, checked_included=True,
                cabin_included=True, baggage_notes=("SIMULATED: 15 kg checked + 7 kg cabin",), fare_verified=True,
                seller="IndiGo", booking_url="https://www.goindigo.in")
    for run, price in ((1, "7400"), (2, "7200")):
        con.execute("INSERT INTO runs (id, started_at, status) VALUES (?, ?, 'sim')", (run, db.now()))
        monitor.process(con, run, [Fare(displayed_inr=Decimal(price), **base)])
    con.execute("UPDATE alerts SET subject='[SIMULATION] ' || subject")
    for r in mailer.flush(con, dry_run=a.dry_run):
        row = con.execute("SELECT subject FROM alerts WHERE id=?", (r["alert"],)).fetchone()
        print(json.dumps({"subject": row[0], **r}, ensure_ascii=False))


def main(argv=None):
    p = argparse.ArgumentParser(prog="tracker")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="authorize Gmail (opens browser)")
    sub.add_parser("init", help="create/upgrade the database")
    c = sub.add_parser("check", help="run one monitoring pass")
    c.add_argument("--dry-run", action="store_true", default=DRY_RUN)
    t = sub.add_parser("test-email")
    t.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("simulate-drop", help="simulate a verified drop below 7,500 and email it")
    s.add_argument("--dry-run", action="store_true")
    tr = sub.add_parser("trial-start")
    tr.add_argument("--hours", type=int, default=24)
    tr.add_argument("--systemd", action="store_true", help="enable the systemd user timer first")
    for name in ("pause", "resume", "booked"):
        x = sub.add_parser(name, help=f"{name} a target (A-D) or all")
        x.add_argument("target", choices=["A", "B", "C", "D", "all"])
    sub.add_parser("summary")
    sub.add_parser("sources")
    f = sub.add_parser("flush", help="retry pending emails")
    f.add_argument("--dry-run", action="store_true", default=DRY_RUN)
    a = p.parse_args(argv)

    if a.cmd == "auth":
        cmd_auth(a)
    elif a.cmd == "init":
        _con()
        print(f"database ready: {DB_PATH}")
    elif a.cmd == "check":
        print(json.dumps(monitor.run_once(DB_PATH, dry_run=a.dry_run), ensure_ascii=False, default=str))
    elif a.cmd == "test-email":
        cmd_test_email(a)
    elif a.cmd == "simulate-drop":
        cmd_simulate_drop(a)
    elif a.cmd == "trial-start":
        print(json.dumps(monitor.start_trial(_con(), a.hours, a.systemd)))
        print(json.dumps(monitor.run_once(DB_PATH), ensure_ascii=False, default=str))
    elif a.cmd in ("pause", "resume", "booked"):
        con = _con()
        col, val = ("booked", 1) if a.cmd == "booked" else ("paused", int(a.cmd == "pause"))
        for tid in ([t.id for t in TARGETS] if a.target == "all" else [a.target]):
            con.execute("INSERT OR IGNORE INTO target_state (target_id) VALUES (?)", (tid,))
            con.execute(f"UPDATE target_state SET {col}=? WHERE target_id=?", (val, tid))
        print(f"{a.cmd}: {a.target}; active targets: {sorted(db.active_targets(con))}")
    elif a.cmd == "summary":
        print(monitor.summary(_con()))
    elif a.cmd == "sources":
        for r in _con().execute("SELECT * FROM sources ORDER BY name"):
            print(json.dumps(dict(r), ensure_ascii=False))
    elif a.cmd == "flush":
        print(json.dumps(mailer.flush(_con(), dry_run=a.dry_run)))


if __name__ == "__main__":
    main()
