"""Fare retrieval. API first (SerpApi Google Flights), Agent Reach web reader as fallback.

Every call returns (status, fares, error). A failure never produces a fare.
Statuses: ok, no-results, requires-authorization, rate-limited, blocked, temporarily-failed, unsupported, skipped.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime
from decimal import Decimal

from .config import AIRLINE_SITES, IST, PLATFORM_SITES, THRESHOLD_INR, Target
from .core import Fare, approved_url, parse_baggage

SERPAPI_URL = "https://serpapi.com/search.json"
# Booking-option lookups cost one API search each; only done for fares that could qualify.
MAX_BOOKING_LOOKUPS = int(os.environ.get("MAX_BOOKING_LOOKUPS", "3"))

AR_GOOGLE = "Google Flights (Agent Reach)"
REGISTRY = [
    # name, domain, method, api_mcp, agent_reach, baggage_verifiable, auth, polling
    ("Google Flights", "google.com", "SerpApi google_flights API (+ booking options)", "SerpApi REST API",
     "fallback: Jina reader on /travel/flights?q= (robots allows)", "yes, via booking options baggage_prices",
     "SERPAPI_API_KEY", "plan quota; hourly x 4 targets"),
    (AR_GOOGLE, "google.com", "Agent Reach web reader (Jina) - used only when SerpApi fails", "n/a",
     "/travel/flights?q= (robots allows)", "no", "none", "Jina anonymous limits"),
    ("Official airline sites", "goindigo.in, airindia.com, airindiaexpress.com, akasaair.com, spicejet.com",
     "booking links only; fares seen via Google Flights seller options", "none public", "not used: JS apps",
     "via seller option text", "none", "n/a"),
    ("MakeMyTrip", "makemytrip.com", "Agent Reach web reader", "none public", "tried", "no", "none", "robots + rate limits"),
    ("ixigo", "ixigo.com", "Agent Reach web reader", "none public", "tried", "no", "none", "robots disallows /search/result/"),
    ("Cleartrip", "cleartrip.com", "Agent Reach web reader", "none public", "tried", "no", "none", "robots disallows /flights/search*"),
    ("Skyscanner", "skyscanner.co.in", "Agent Reach web reader", "partner API only (approval needed)", "tried", "no",
     "partner key", "robots disallows /transport/*"),
]


def seed_registry(con) -> None:
    for name, domain, method, api, ar, bag, auth, poll in REGISTRY:
        con.execute("INSERT OR IGNORE INTO sources (name,approved,domain,method,api_mcp,agent_reach,fare_tested,"
                    "fares_usable,baggage_verifiable,auth,polling,status) VALUES (?,?,?,?,?,?,0,0,?,?,?,?)",
                    (name, 1, domain, method, api, ar, bag, auth, poll, "configured-but-not-verified"))


# ---------- SerpApi (Google Flights) ----------

def _get_json(params: dict, opener=urllib.request.urlopen, tries: int = 3) -> tuple[int, dict]:
    url = SERPAPI_URL + "?" + urllib.parse.urlencode(params)
    for attempt in range(tries):
        try:
            with opener(url, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                data = json.loads(body)
            except ValueError:
                data = {"error": f"HTTP {e.code}"}
            if e.code < 500 or attempt == tries - 1:
                return e.code, data
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == tries - 1:
                return 0, {"error": f"network: {type(e).__name__}"}
        time.sleep(2 ** attempt)
    return 0, {"error": "unreachable"}


def _classify(code: int, data: dict) -> tuple[str, str | None]:
    err = data.get("error")
    if code == 200 and not err:
        return "ok", None
    if err and "hasn't returned any results" in err:
        return "no-results", None
    if code == 401 or (err and "api key" in err.lower()):
        return "requires-authorization", "SerpApi rejected the API key"
    if code == 429 or (err and ("run out of searches" in err or "rate" in err.lower())):
        return "rate-limited", err
    return "temporarily-failed", err or f"HTTP {code}"


def gflights_link(t: Target) -> str:
    q = f"Flights to {t.dest} from {t.origin} on {t.date.isoformat()} one way"
    return "https://www.google.com/travel/flights?" + urllib.parse.urlencode({"q": q, "curr": "INR", "hl": "en"})


def _ist(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=IST)  # Indian airports report local IST


def _itinerary_fare(t: Target, it: dict) -> Fare | None:
    segs = it.get("flights") or []
    if not segs or it.get("price") is None:
        return None
    airlines = {s.get("airline") for s in segs}
    notes = list(it.get("extensions") or []) + [x for s in segs for x in (s.get("extensions") or [])]
    bag = parse_baggage(notes)
    return Fare(
        target_id=t.id, source="Google Flights", source_flight_id=it.get("booking_token", "")[:64],
        flight_numbers=tuple(s.get("flight_number", "?") for s in segs),
        airline=airlines.pop() if len(airlines) == 1 else "Multiple airlines",
        origin=segs[0]["departure_airport"]["id"], dest=segs[-1]["arrival_airport"]["id"],
        depart=_ist(segs[0]["departure_airport"]["time"]), arrive=_ist(segs[-1]["arrival_airport"]["time"]),
        stops=len(it.get("layovers") or []), duration_min=it.get("total_duration"),
        displayed_inr=Decimal(str(it["price"])), baggage_notes=tuple(notes), booking_url=gflights_link(t),
        raw={"itinerary": it}, **bag)


def _approved_seller(opt: dict) -> str | None:
    """Return the booking URL for an approved seller, else None (unknown sellers are dropped)."""
    seller = (opt.get("book_with") or "").strip()
    if opt.get("airline") and seller in AIRLINE_SITES:
        return AIRLINE_SITES[seller]
    for name, url in PLATFORM_SITES.items():
        if seller.lower() == name.lower():
            return url
    return None


def _verify_with_booking_options(t: Target, fare: Fare, key: str, opener) -> Fare:
    code, data = _get_json({"engine": "google_flights", "booking_token": fare.raw["itinerary"]["booking_token"],
                            "departure_id": t.origin, "arrival_id": t.dest, "outbound_date": t.date.isoformat(),
                            "type": 2, "currency": "INR", "hl": "en", "gl": "in", "api_key": key}, opener)
    if _classify(code, data)[0] != "ok":
        return fare
    best = None
    for o in data.get("booking_options") or []:
        opt = o.get("together") or o.get("departing") or {}
        url = _approved_seller(opt)
        if not url or opt.get("price") is None or not approved_url(url):
            continue
        if best is None or opt["price"] < best[0]["price"]:
            best = (opt, url)
    if not best:
        return fare
    opt, url = best
    notes = list(opt.get("extensions") or []) + list(opt.get("baggage_prices") or [])
    bag = parse_baggage(notes)
    fare.displayed_inr = Decimal(str(opt["price"]))
    fare.seller, fare.booking_url, fare.fare_verified = opt.get("book_with"), url, True
    fare.baggage_notes = tuple(notes) + (f"fare: {opt.get('option_title')}",) if opt.get("option_title") else tuple(notes)
    fare.checked_included, fare.cabin_included, fare.checked_bag_inr = (
        bag["checked_included"], bag["cabin_included"], bag["checked_bag_inr"])
    fare.raw["booking_option"] = opt
    return fare


def quota_left(key: str, opener=urllib.request.urlopen) -> int | None:
    """Account endpoint is free (does not consume searches). None = unknown, proceed."""
    try:
        with opener("https://serpapi.com/account.json?" + urllib.parse.urlencode({"api_key": key}), timeout=30) as r:
            return int(json.loads(r.read())["total_searches_left"])
    except Exception:
        return None


def serpapi_fetch(t: Target, opener=urllib.request.urlopen) -> tuple[str, list[Fare], str | None]:
    key = os.environ.get("SERPAPI_API_KEY")
    if not key:
        return "requires-authorization", [], "SERPAPI_API_KEY not set"
    left = quota_left(key, opener)
    if left is not None and left < 1 + MAX_BOOKING_LOOKUPS:
        return "rate-limited", [], f"SerpApi quota nearly exhausted ({left} searches left)"
    code, data = _get_json({"engine": "google_flights", "departure_id": t.origin, "arrival_id": t.dest,
                            "outbound_date": t.date.isoformat(), "type": 2, "adults": 1, "travel_class": 1,
                            "currency": "INR", "hl": "en", "gl": "in", "api_key": key}, opener)
    status, err = _classify(code, data)
    if status != "ok":
        return status, [], err
    if (data.get("search_parameters") or {}).get("currency", "INR") != "INR":
        return "temporarily-failed", [], "response not in INR"
    fares = [f for it in (data.get("best_flights") or []) + (data.get("other_flights") or [])
             if (f := _itinerary_fare(t, it))]
    lookups = 0
    for f in sorted(fares, key=lambda f: f.displayed_inr):
        if f.displayed_inr >= THRESHOLD_INR or lookups >= MAX_BOOKING_LOOKUPS:
            break
        if f.raw["itinerary"].get("booking_token"):
            lookups += 1
            _verify_with_booking_options(t, f, key, opener)
    return ("ok" if fares else "no-results"), fares, None


# ---------- Agent Reach fallback (documented interface: curl https://r.jina.ai/URL) ----------

def agent_reach_urls(t: Target) -> dict[str, str]:
    d = t.date
    return {
        "Google Flights": gflights_link(t),
        "MakeMyTrip": f"https://www.makemytrip.com/flight/search?itinerary={t.origin}-{t.dest}-{d:%d/%m/%Y}"
                      "&tripType=O&paxType=A-1_C-0_I-0&intl=false&cabinClass=E",
        "ixigo": f"https://www.ixigo.com/search/result/flight?from={t.origin}&to={t.dest}&date={d:%d%m%Y}&adults=1",
        "Cleartrip": f"https://www.cleartrip.com/flights/results?adults=1&depart_date={d:%d/%m/%Y}"
                     f"&from={t.origin}&to={t.dest}&intl=n",
        "Skyscanner": f"https://www.skyscanner.co.in/transport/flights/{t.origin.lower()}/{t.dest.lower()}/{d:%y%m%d}/",
    }


def robots_allows(url: str, fetch=None) -> bool:
    parts = urllib.parse.urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser(robots_url)
    try:
        rp.parse((fetch or _fetch_text)(robots_url).splitlines())
    except urllib.error.HTTPError as e:
        return 400 <= e.code < 500 and e.code not in (401, 403)  # no robots.txt = allowed; denied = not
    except (OSError, TimeoutError):
        return False  # unknown rules: do not fetch
    return rp.can_fetch("*", url)


def _fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def agent_reach_read(url: str, run=subprocess.run) -> tuple[str, str, str | None]:
    """Read a page with Agent Reach's web channel. Returns (status, markdown, error)."""
    try:
        p = run(["curl", "-s", "-m", "60", "-w", "\n%{http_code}", f"https://r.jina.ai/{url}"],
                capture_output=True, text=True, timeout=90, check=False)
    except subprocess.TimeoutExpired:
        return "temporarily-failed", "", "timeout"
    if p.returncode != 0:
        return "temporarily-failed", "", f"curl exit {p.returncode}"
    body, _, code = p.stdout.rpartition("\n")
    low = body.lower()
    if code == "429" or "too many requests" in low:
        return "rate-limited", "", "rate limited (Agent Reach: stop, do not retry)"
    if code in ("401", "403", "451") or "abusealleviation" in low or "returned error 403" in low or "captcha" in low:
        return "blocked", "", f"blocked (HTTP {code})"
    if code != "200":
        return "temporarily-failed", "", f"HTTP {code}"
    return "ok", body, None


def agent_reach_fetch(url: str, run=subprocess.run, robots=robots_allows) -> tuple[str, list[Fare], str | None]:
    if not robots(url):
        return "blocked", [], "robots.txt disallows this path"
    status, _, err = agent_reach_read(url, run)
    if status != "ok":
        return status, [], err
    # Reader output has no flight numbers/times tied to prices; never turn loose ₹ amounts into fares.
    return "unsupported", [], "page read but contains no structured fare data (JS-rendered results)"
