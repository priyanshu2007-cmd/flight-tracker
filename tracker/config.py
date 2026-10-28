"""Fixed travel requirements and source policy. Dates are never derived from 'today'."""
import os
from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
THRESHOLD_INR = Decimal("7500")  # strict: price < threshold qualifies, per one-way ticket
# Smallest fall (INR) below the last alerted price that counts as a new drop. Default 1 = every real drop.
MIN_DROP_INR = Decimal(os.environ.get("MIN_DROP_INR", "1"))
ALERT_TO = os.environ.get("ALERT_TO", "spec.priyanshu@gmail.com")
DB_PATH = os.environ.get("TRACKER_DB", "data/tracker.db")
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"


@dataclass(frozen=True)
class Target:
    id: str
    origin: str
    dest: str
    date: date
    depart_after: time | None = None  # strict: departure must be > this IST time


TARGETS = (
    Target("A", "BLR", "LKO", date(2026, 12, 23), depart_after=time(17, 0)),
    Target("B", "BLR", "LKO", date(2026, 12, 24)),
    Target("C", "LKO", "BLR", date(2027, 1, 4)),
    Target("D", "LKO", "BLR", date(2027, 1, 5)),
)

# Official airline domains (checked by hand against each airline's own site) and approved platforms.
AIRLINE_SITES = {
    "IndiGo": "https://www.goindigo.in",
    "Air India": "https://www.airindia.com",
    "Air India Express": "https://www.airindiaexpress.com",
    "Akasa Air": "https://www.akasaair.com",
    "SpiceJet": "https://www.spicejet.com",
}
PLATFORM_SITES = {
    "Google Flights": "https://www.google.com/travel/flights",
    "Skyscanner": "https://www.skyscanner.co.in",
    "MakeMyTrip": "https://www.makemytrip.com",
    "ixigo": "https://www.ixigo.com",
    "Cleartrip": "https://www.cleartrip.com",
}
APPROVED_DOMAINS = (
    "goindigo.in", "airindia.com", "airindiaexpress.com", "akasaair.com", "spicejet.com",
    "google.com", "skyscanner.co.in", "skyscanner.net", "makemytrip.com", "ixigo.com", "cleartrip.com",
)
