"""SQLite persistence: targets, observations, source health, runs, alert state, outbox, trial."""
import json
import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

from .config import IST, TARGETS

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (id TEXT PRIMARY KEY, origin TEXT, dest TEXT, travel_date TEXT, depart_after TEXT);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY, run_id INTEGER, target_id TEXT, flight_key TEXT, source TEXT, source_flight_id TEXT,
  airline TEXT, flight_numbers TEXT, origin TEXT, dest TEXT, depart TEXT, arrive TEXT, stops INTEGER,
  duration_min INTEGER, base_fare TEXT, taxes TEXT, displayed_inr TEXT, checked_bag_inr TEXT, comparable_inr TEXT,
  currency TEXT, checked_included INTEGER, cabin_included INTEGER, baggage_notes TEXT, baggage_verified INTEGER,
  fare_verified INTEGER, verification_state TEXT, seller TEXT, booking_url TEXT, observed_at TEXT, raw TEXT);
CREATE INDEX IF NOT EXISTS obs_key ON observations(flight_key, observed_at);
CREATE TABLE IF NOT EXISTS source_checks (
  id INTEGER PRIMARY KEY, run_id INTEGER, source TEXT, target_id TEXT, status TEXT, fares INTEGER, error TEXT, checked_at TEXT);
CREATE TABLE IF NOT EXISTS sources (
  name TEXT PRIMARY KEY, approved INTEGER, domain TEXT, method TEXT, api_mcp TEXT, agent_reach TEXT,
  fare_tested INTEGER, fares_usable INTEGER, baggage_verifiable TEXT, auth TEXT, polling TEXT,
  status TEXT, last_success TEXT, failure_reason TEXT, blocked_until TEXT);
CREATE TABLE IF NOT EXISTS alert_state (flight_key TEXT, kind TEXT, last_alerted TEXT, below INTEGER, PRIMARY KEY (flight_key, kind));
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY, idem_key TEXT UNIQUE, flight_key TEXT, kind TEXT, price TEXT, prev_price TEXT,
  subject TEXT, body TEXT, status TEXT, attempts INTEGER DEFAULT 0, provider_id TEXT, error TEXT,
  created_at TEXT, sent_at TEXT);
CREATE TABLE IF NOT EXISTS target_state (target_id TEXT PRIMARY KEY, paused INTEGER DEFAULT 0, booked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS trial (id INTEGER PRIMARY KEY CHECK (id = 1), started_at TEXT, ends_at TEXT, status TEXT, summary TEXT);
"""


def now() -> str:
    """Stored timestamps are UTC ISO-8601; convert with ist() for display."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def ist(s: str | None) -> str:
    return datetime.fromisoformat(s).astimezone(IST).strftime("%d %b %Y %H:%M IST") if s else "never"


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit; explicit BEGIN where atomic
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    for t in TARGETS:
        con.execute("INSERT OR REPLACE INTO targets VALUES (?,?,?,?,?)",
                    (t.id, t.origin, t.dest, t.date.isoformat(), t.depart_after and t.depart_after.isoformat()))
    return con


def _s(v):
    return None if v is None else str(v)


def add_observation(con, run_id, f) -> None:
    con.execute(
        "INSERT INTO observations (run_id,target_id,flight_key,source,source_flight_id,airline,flight_numbers,origin,dest,"
        "depart,arrive,stops,duration_min,base_fare,taxes,displayed_inr,checked_bag_inr,comparable_inr,currency,"
        "checked_included,cabin_included,baggage_notes,baggage_verified,fare_verified,verification_state,seller,booking_url,"
        "observed_at,raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, f.target_id, f.key, f.source, f.source_flight_id, f.airline, "+".join(f.flight_numbers), f.origin,
         f.dest, f.depart.isoformat(), f.arrive and f.arrive.isoformat(), f.stops, f.duration_min, _s(f.base_fare),
         _s(f.taxes), str(f.displayed_inr), _s(f.checked_bag_inr), _s(f.comparable_inr), f.currency,
         f.checked_included, f.cabin_included, json.dumps(list(f.baggage_notes)), f.baggage_verified,
         f.fare_verified, f.verification_state, f.seller, f.booking_url,
         f.observed_at.astimezone(UTC).isoformat(timespec="seconds"), json.dumps(f.raw, default=str)))


def history(con, flight_key: str, kind: str, before_run: int) -> dict:
    """Previous and lowest earlier price for a flight, in the same metric the alert uses."""
    col = "comparable_inr" if kind == "deal" else "displayed_inr"
    rows = con.execute(f"SELECT {col}, observed_at FROM observations WHERE flight_key=? AND run_id<? AND {col} IS NOT NULL"
                       " ORDER BY id", (flight_key, before_run)).fetchall()
    if not rows:
        return {}
    low = min(rows, key=lambda r: Decimal(r[0]))
    return {"prev": Decimal(rows[-1][0]), "lowest": Decimal(low[0]), "lowest_at": low[1]}


def active_targets(con) -> set[str]:
    """Targets not paused and not marked booked."""
    off = {r[0] for r in con.execute("SELECT target_id FROM target_state WHERE paused=1 OR booked=1")}
    return {t.id for t in TARGETS} - off


def get_state(con, key, kind):
    r = con.execute("SELECT last_alerted, below FROM alert_state WHERE flight_key=? AND kind=?", (key, kind)).fetchone()
    return r and {"last_alerted": r[0] and Decimal(r[0]), "below": bool(r[1])}


def put_state(con, key, kind, st) -> None:
    con.execute("INSERT OR REPLACE INTO alert_state VALUES (?,?,?,?)", (key, kind, _s(st["last_alerted"]), st["below"]))


def check(con, run_id, source, target_id, status, fares=0, error=None) -> None:
    con.execute("INSERT INTO source_checks (run_id,source,target_id,status,fares,error,checked_at) VALUES (?,?,?,?,?,?,?)",
                (run_id, source, target_id, status, fares, error, now()))
