import io
import json
import pathlib
import subprocess
import urllib.error
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from tracker import db, mailer, monitor, sources
from tracker.config import IST, TARGETS
from tracker.core import Fare, approved_url, decide, eligible, parse_baggage

T = {t.id: t for t in TARGETS}
ROOT = pathlib.Path(__file__).resolve().parents[1]


def fare(target="B", price="7000", hh=9, mm=0, fn="6E 101", verified=True, checked=True, cabin=True, bag=None, **kw):
    t = T[target]
    dep = datetime.combine(t.date, datetime.min.time(), IST) + timedelta(hours=hh, minutes=mm)
    args = dict(target_id=target, source="Google Flights", source_flight_id="x", flight_numbers=(fn,),
                airline="IndiGo", origin=t.origin, dest=t.dest, depart=dep, arrive=dep + timedelta(hours=2, minutes=40),
                stops=0, duration_min=160, displayed_inr=Decimal(price), checked_included=checked, cabin_included=cabin,
                checked_bag_inr=bag, fare_verified=verified, seller="IndiGo", booking_url="https://www.goindigo.in")
    args.update(kw)
    return Fare(**args)


@pytest.fixture
def con():
    c = db.connect(":memory:")
    sources.seed_registry(c)
    return c


def run(con, fares):
    rid = con.execute("INSERT INTO runs (started_at, status) VALUES (?, 'ok')", (db.now(),)).lastrowid
    return monitor.process(con, rid, fares)


# 1-4 eligibility
def test_dec23_at_or_before_5pm_rejected():
    assert not eligible(fare("A", hh=17, mm=0), T["A"])
    assert not eligible(fare("A", hh=9), T["A"])


def test_dec23_after_5pm_eligible():
    assert eligible(fare("A", hh=17, mm=1), T["A"])


def test_dec24_any_time_eligible():
    assert eligible(fare("B", hh=0, mm=5), T["B"]) and eligible(fare("B", hh=23, mm=55), T["B"])


def test_return_targets_exact_and_independent(con):
    assert (T["C"].date, T["D"].date) == (date(2027, 1, 4), date(2027, 1, 5))
    assert (T["C"].origin, T["C"].dest) == ("LKO", "BLR")
    assert not eligible(fare("C"), T["D"])  # Jan 4 flight is not a Jan 5 flight
    assert len(run(con, [fare("C", fn="6E 1"), fare("D", fn="6E 1")])) == 2


# 5-6 threshold per ticket, strict
def test_threshold_strict_per_ticket(con):
    assert decide(None, Decimal("7499"))[0] is True
    assert decide(None, Decimal("7500"))[0] is False
    assert len(run(con, [fare("B", "7499", fn="X1"), fare("C", "7499", fn="X1")])) == 2  # not summed


# 7-10 drop logic
def test_first_then_drop_then_unchanged_then_increase(con):
    assert len(run(con, [fare(price="7300")])) == 1
    assert len(run(con, [fare(price="7300")])) == 0  # unchanged hourly check
    assert len(run(con, [fare(price="7400")])) == 0  # increase is not a drop
    subj = run(con, [fare(price="7100")])
    assert len(subj) == 1 and "₹7,100" in subj[0]
    body = con.execute("SELECT body FROM alerts ORDER BY id DESC").fetchone()[0]
    assert "Previous observed: ₹7,400" in body and "New lower price" in body


def test_above_then_below_alerts_and_recrossing(con):
    assert run(con, [fare(price="8000")]) == []
    assert len(run(con, [fare(price="7200")])) == 1
    run(con, [fare(price="7600")])
    assert len(run(con, [fare(price="7200")])) == 1  # fell back below the threshold


def test_different_flight_gets_own_alert(con):
    run(con, [fare(price="7000", fn="6E 1")])
    assert len(run(con, [fare(price="7200", fn="AI 2")])) == 1


# 11 duplicates across sources
def test_same_flight_two_sources_one_alert(con):
    a, b = fare(price="7000"), fare(price="7000", source="Other")
    assert len(run(con, [a, b])) == 1
    assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2  # both kept for audit


# 12-13 baggage
def test_checked_bag_charge_added():
    f = fare(price="6800", checked=False, bag=Decimal("900"))
    assert f.comparable_inr == Decimal("7700") and f.alert_kind == "deal"


def test_bag_charge_pushes_over_threshold_no_deal(con):
    assert run(con, [fare(price="6800", checked=False, bag=Decimal("900"))]) == []


def test_unknown_baggage_is_lead_not_deal(con):
    f = fare(price="6000", checked=None)
    assert f.comparable_inr is None and f.alert_kind == "lead"
    subj = run(con, [f])
    assert len(subj) == 1 and subj[0].startswith("UNVERIFIED LEAD")


def test_parse_baggage_explicit_only():
    assert parse_baggage(["1 free carry-on", "1st checked bag: ₹1,500"]) == {
        "cabin_included": True, "checked_included": False, "checked_bag_inr": Decimal("1500")}
    assert parse_baggage(["Wi-Fi", "Average legroom"]) == {
        "cabin_included": None, "checked_included": None, "checked_bag_inr": None}
    assert parse_baggage(["Checked baggage for a fee"])["checked_included"] is False


# 14 persistence across restart
def test_history_and_state_survive_restart(tmp_path):
    p = str(tmp_path / "t.db")
    c1 = db.connect(p)
    run(c1, [fare(price="7300")])
    c1.close()
    c2 = db.connect(p)
    assert run(c2, [fare(price="7300")]) == []
    assert c2.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2


# 15-16 Agent Reach interface and documented block handling
def fake_curl(body, code):
    return lambda args, **kw: subprocess.CompletedProcess(args, 0, f"{body}\n{code}", "")


def test_agent_reach_uses_documented_reader():
    seen = {}

    def spy(args, **kw):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, "Title: x\n200", "")
    sources.agent_reach_read("https://www.google.com/travel/flights?q=x", run=spy)
    assert seen["args"][0] == "curl" and seen["args"][-1].startswith("https://r.jina.ai/https://www.google.com/")


@pytest.mark.parametrize("body,code,status", [
    ('{"name":"AbuseAlleviationError"}', "403", "blocked"),
    ("Title: Oops...too many requests !", "200", "rate-limited"),
    ("", "429", "rate-limited"),
    ("", "502", "temporarily-failed"),
    ("Markdown Content: flights", "200", "unsupported"),
])
def test_agent_reach_statuses_never_produce_fares(body, code, status):
    st, fares, _ = sources.agent_reach_fetch("https://www.makemytrip.com/x", run=fake_curl(body, code),
                                             robots=lambda u: True)
    assert st == status and fares == []


def test_robots_disallow_skips_fetch():
    called = []
    st, _, err = sources.agent_reach_fetch("https://www.ixigo.com/search/result/flight?x=1",
                                           run=lambda *a, **k: called.append(1), robots=lambda u: False)
    assert st == "blocked" and not called and "robots" in err


def test_robots_parser_real_rules():
    rules = "User-agent: *\nDisallow: /travel/flights/search\nDisallow: /search/result/\n"
    assert sources.robots_allows("https://www.google.com/travel/flights?q=a", fetch=lambda u: rules)
    assert not sources.robots_allows("https://www.ixigo.com/search/result/flight", fetch=lambda u: rules)


def test_block_pauses_path_for_24h(con):
    calls = []

    def reach(url):
        calls.append(url)
        return "blocked", [], "blocked (HTTP 403)"
    serp = lambda t: ("requires-authorization", [], "no key")
    monitor.collect(con, 1, serp=serp, reach=reach)
    n = len(calls)
    monitor.collect(con, 2, serp=serp, reach=reach)
    assert n > 0 and len(calls) == n  # no retry while blocked


# 17-18 approved sources only
def test_unapproved_domains_rejected():
    assert approved_url("https://www.goindigo.in/booking")
    assert approved_url("https://www.google.com/travel/flights?q=x")
    for bad in ("https://cheap-flights-now.xyz/", "http://www.goindigo.in/", "https://goindigo.in.evil.com/",
                "https://evilgoindigo.in/", None):
        assert not approved_url(bad)


def test_unknown_seller_dropped():
    assert sources._approved_seller({"book_with": "FlyCheapz", "airline": False}) is None
    assert sources._approved_seller({"book_with": "IndiGo", "airline": True}) == "https://www.goindigo.in"
    assert sources._approved_seller({"book_with": "MakeMyTrip"}) == "https://www.makemytrip.com"


# SerpApi parsing with mocked responses (integration)
SEARCH = {"search_parameters": {"currency": "INR"}, "best_flights": [
    {"flights": [{"departure_airport": {"id": "BLR", "time": "2026-12-24 06:00"},
                  "arrival_airport": {"id": "LKO", "time": "2026-12-24 08:40"}, "airline": "IndiGo",
                  "flight_number": "6E 2088", "extensions": []}],
     "layovers": [], "total_duration": 160, "price": 6900, "booking_token": "tok1"},
    {"flights": [{"departure_airport": {"id": "BLR", "time": "2026-12-24 10:00"},
                  "arrival_airport": {"id": "LKO", "time": "2026-12-24 12:45"}, "airline": "Air India",
                  "flight_number": "AI 503", "extensions": []}],
     "layovers": [], "total_duration": 165, "price": 9100, "booking_token": "tok2"}]}
QUOTA = {"total_searches_left": 100}
BOOKING = {"booking_options": [
    {"together": {"book_with": "FlyCheapz", "airline": False, "price": 5000}},
    {"together": {"book_with": "IndiGo", "airline": True, "price": 6950, "option_title": "Saver",
                  "extensions": ["1 free carry-on", "1 free checked bag"], "baggage_prices": []}}]}


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_for(*payloads):
    it = iter(payloads)

    def op(url, timeout):
        p = next(it)
        if isinstance(p, Exception):
            raise p
        return Resp(json.dumps(p).encode())
    return op


def test_serpapi_parse_and_verify(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    st, fares, _ = sources.serpapi_fetch(T["B"], opener=opener_for(QUOTA, SEARCH, BOOKING))
    assert st == "ok" and len(fares) == 2
    f = next(x for x in fares if x.flight_numbers == ("6E 2088",))
    assert f.fare_verified and f.seller == "IndiGo" and f.displayed_inr == Decimal("6950")  # unknown seller ignored
    assert f.alert_kind == "deal" and f.comparable_inr == Decimal("6950")
    g = next(x for x in fares if x.flight_numbers == ("AI 503",))
    assert not g.fare_verified and g.alert_kind == "lead"  # above threshold: no lookup spent


def test_serpapi_failures_are_not_fares(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    assert sources.serpapi_fetch(T["B"])[0:2] == ("requires-authorization", [])
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    err = urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b'{"error":"Your account has run out of searches."}'))
    assert sources.serpapi_fetch(T["B"], opener=opener_for(QUOTA, err))[0:2] == ("rate-limited", [])
    nores = {"error": "Google Flights hasn't returned any results for this query."}
    assert sources.serpapi_fetch(T["B"], opener=opener_for(QUOTA, nores))[0:2] == ("no-results", [])


# 19 failures are recorded as checks, not observations
def test_source_failure_not_observation(con):
    fares = monitor.collect(con, 1, serp=lambda t: ("temporarily-failed", [], "HTTP 500"),
                            reach=lambda u: ("blocked", [], "x"))
    assert fares == [] and con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM source_checks WHERE status='temporarily-failed'").fetchone()[0] == 4


# 20-21 Gmail failure/retry and dry-run
class FakeSender:
    def __init__(self, results):
        self.results, self.sent = list(results), []

    def send(self, subject, body):
        self.sent.append(subject)
        return self.results.pop(0)


def test_gmail_failure_kept_and_retried(con):
    run(con, [fare(price="7000")])
    s = FakeSender([{"status": "failed", "id": None, "error": "HTTP 503"}, {"status": "delivered", "id": "m1", "error": None}])
    mailer.flush(con, sender=s)
    assert con.execute("SELECT status, attempts FROM alerts").fetchone()[:] == ("failed", 1)
    mailer.flush(con, sender=s)
    assert con.execute("SELECT status, provider_id, attempts FROM alerts").fetchone()[:] == ("delivered", "m1", 2)
    mailer.flush(con, sender=s)
    assert len(s.sent) == 2  # delivered alerts are never resent


def test_dry_run_never_sends(con):
    run(con, [fare(price="7000")])
    s = FakeSender([])
    assert mailer.flush(con, sender=s, dry_run=True)[0]["status"] == "dry-run" and s.sent == []


def test_missing_token_keeps_alert_pending(con, monkeypatch):
    run(con, [fare(price="7000")])
    monkeypatch.setattr(mailer, "TOKEN_FILE", "/nonexistent/token.json")
    r = mailer.flush(con)[0]
    assert r["status"] == "failed" and r["error"].startswith("auth:")


# 22 trial lifecycle, overlap lock, end-to-end dry run
def test_trial_ends_and_stops(tmp_path, monkeypatch):
    p = str(tmp_path / "t.db")
    c = db.connect(p)
    monitor.start_trial(c, hours=24)
    c.execute("UPDATE trial SET ends_at=? WHERE id=1", ((datetime.now(IST) - timedelta(minutes=1)).isoformat(),))
    calls = []
    monkeypatch.setattr(monitor.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    r = monitor.run_once(p, dry_run=True)
    assert r["status"] == "trial-ended" and "Target A" in r["summary"]
    assert any("disable" in c for c in calls)
    assert monitor.run_once(p, dry_run=True)["status"] == "skipped"
    assert c.execute("SELECT status FROM trial").fetchone()[0] == "completed"


def test_trial_length_is_24h(con):
    tr = monitor.start_trial(con)
    s, e = datetime.fromisoformat(tr["started_at"]), datetime.fromisoformat(tr["ends_at"])
    assert e - s == timedelta(hours=24)


def test_overlapping_runs_prevented(tmp_path):
    lock = str(tmp_path / "run.lock")
    with monitor.run_lock(lock) as a:
        with monitor.run_lock(lock) as b:
            assert a is True and b is False


def test_end_to_end_dry_run(tmp_path, monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    p = str(tmp_path / "e2e.db")
    serp = lambda t: sources.serpapi_fetch(t, opener=opener_for(QUOTA, SEARCH, BOOKING)) if t.id == "B" else ("no-results", [], None)
    reach = lambda u: ("blocked", [], "robots.txt disallows this path")
    r = monitor.run_once(p, dry_run=True, serp=serp, reach=reach)
    assert r["status"] == "ok" and r["fares"] == 2 and len(r["queued"]) == 1
    assert r["sent"][0]["status"] == "dry-run"
    r2 = monitor.run_once(p, dry_run=True, serp=serp, reach=reach)
    assert r2["queued"] == []  # same price next hour: silence
    c = db.connect(p)
    assert c.execute("SELECT status FROM sources WHERE name='Google Flights'").fetchone()[0] == "verified"


# 23-24 secrets and forbidden capabilities
def test_no_secrets_or_forbidden_paths_in_repo():  # code + config only; docs state the prohibitions
    text = "\n".join(p.read_text() for p in ROOT.rglob("*") if p.suffix in (".py", ".toml", ".example", ".service",
                                                                            ".timer", ".sh", ".yml")
                     and ".venv" not in p.parts and "tests" not in p.parts)
    for bad in ("whatsapp", "twilio", "checkout(", "def book", "purchase(", "refresh_token\":", "ya29."):
        assert bad not in text.lower()
    env = (ROOT / ".env.example").read_text()
    assert "your-serpapi-key" in env


def test_email_never_contains_token_and_warns(con):
    run(con, [fare(price="7000")])
    body = con.execute("SELECT body FROM alerts").fetchone()[0]
    assert "can change before you book" in body and "never books" in body and "api_key" not in body


# spec additions: verification states, quota guard, pause/booked, UTC storage
def test_verification_states():
    assert fare(checked=True).verification_state == "verified_total"
    assert fare(checked=False, bag=Decimal("900")).verification_state == "verified_but_baggage_extra"
    assert fare(checked=None).verification_state == "baggage_cost_unknown"
    assert fare(verified=False).verification_state == "discovery_only"


def test_quota_guard_stops_before_spending(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    st, fares, err = sources.serpapi_fetch(T["B"], opener=opener_for({"total_searches_left": 2}))
    assert st == "rate-limited" and fares == [] and "quota" in err


def test_paused_and_booked_targets_skipped(con):
    con.execute("INSERT INTO target_state (target_id, paused, booked) VALUES ('A',1,0), ('C',0,1)")
    seen = []
    monitor.collect(con, 1, serp=lambda t: (seen.append(t.id), ("no-results", [], None))[1],
                    reach=lambda u: ("blocked", [], "x"))
    assert seen == ["B", "D"]


def test_timestamps_stored_utc(con):
    run(con, [fare(price="7000")])
    obs = con.execute("SELECT observed_at FROM observations").fetchone()[0]
    assert obs.endswith("+00:00") and db.now().endswith("+00:00")
    assert db.ist(obs).endswith("IST")


def test_actions_workflow_safe_and_hourly():
    wf = (ROOT / ".github/workflows/tracker.yml").read_text()
    assert "cancel-in-progress: false" in wf and "default: loop" in wf  # no overlap, loop is the default
    assert "${{ secrets.SERPAPI_API_KEY }}" in wf and "secrets.GMAIL_TOKEN_JSON" in wf  # secrets only via store
    assert "Trial: completed" in wf and "gh workflow disable" in wf  # schedule stops after the trial
    assert "git show origin/data:tracker.db" in wf and "wal_checkpoint" in wf  # history persists between runs
    assert "Start next loop" in wf and "steps.loop.outcome == 'success'" in wf  # self-chaining, no crash spin
    assert "upload-artifact" in wf and "retention-days: 30" in wf  # point-in-time backups


def test_require_trial_blocks_checks_before_start(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIRE_TRIAL", "1")
    called = []
    r = monitor.run_once(str(tmp_path / "t.db"), dry_run=True, serp=lambda t: called.append(t), reach=lambda u: called.append(u))
    assert r["status"] == "skipped" and called == []


def test_status_email_only_during_trial_and_once_per_hour(con):
    assert monitor.status_email(con, dry_run=True)["status"] == "skipped"
    monitor.start_trial(con)
    run(con, [fare(price="9000")])
    t0 = monitor.STATUS_ANCHOR + timedelta(minutes=1)
    assert monitor.status_email(con, dry_run=True, now=t0 - timedelta(minutes=5))["status"] == "skipped"  # before 13:45 IST
    r = monitor.status_email(con, dry_run=True, now=t0)
    assert r["status"] == "ok" and r["sent"][0]["status"] == "dry-run"
    assert monitor.status_email(con, dry_run=True, now=t0 + timedelta(minutes=90))["status"] == "skipped"  # same slot
    assert monitor.status_email(con, dry_run=True, now=t0 + timedelta(hours=2))["status"] == "ok"  # next slot, 15:45 IST
    assert con.execute("SELECT COUNT(*) FROM alerts WHERE kind='status'").fetchone()[0] == 2
    body = con.execute("SELECT body FROM alerts WHERE kind='status' ORDER BY id").fetchone()[0]
    assert "Target B" in body and "₹9,000" in body


def test_loop_runs_hourly_checks_and_stops_with_trial(tmp_path):
    p = str(tmp_path / "t.db")
    monitor.start_trial(db.connect(p), hours=24)
    clock = {"t": monitor.STATUS_ANCHOR + timedelta(minutes=2)}
    seen = []
    serp = lambda t: (seen.append(clock["t"]), ("no-results", [], None))[1]
    r = monitor.loop(p, minutes=130, dry_run=True, tick=60, clock=lambda: clock["t"],
                     sleep=lambda s: clock.update(t=clock["t"] + timedelta(seconds=s)),
                     serp=serp, reach=lambda u: ("blocked", [], "x"))
    assert r["status"] == "loop-complete" and r["checks"] == 3  # minute 0, 60, 120
    c = db.connect(p)
    assert c.execute("SELECT COUNT(*) FROM alerts WHERE kind='status'").fetchone()[0] == 2  # 13:45 and 15:45 IST slots


def test_loop_exits_without_running_trial(tmp_path):
    assert monitor.loop(str(tmp_path / "t.db"), minutes=5, dry_run=True)["status"] == "stopped"
