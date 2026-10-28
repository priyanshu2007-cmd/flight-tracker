"""Deterministic fare rules: eligibility, baggage-adjusted price, approved URLs, alert decisions."""
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from .config import APPROVED_DOMAINS, IST, MIN_DROP_INR, THRESHOLD_INR, Target


@dataclass
class Fare:
    target_id: str
    source: str
    source_flight_id: str
    flight_numbers: tuple[str, ...]
    airline: str | None
    origin: str
    dest: str
    depart: datetime  # IST-aware
    arrive: datetime | None
    stops: int
    duration_min: int | None
    displayed_inr: Decimal  # total ticket price shown by the seller (taxes included)
    currency: str = "INR"
    base_fare: Decimal | None = None  # unknown unless the source splits it
    taxes: Decimal | None = None
    checked_included: bool | None = None  # None = unknown
    cabin_included: bool | None = None
    checked_bag_inr: Decimal | None = None  # extra charge for one checked bag, when known
    baggage_notes: tuple[str, ...] = ()
    fare_verified: bool = False  # True only for a seller price fetched during this run
    seller: str | None = None
    booking_url: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(IST))
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def key(self) -> str:
        """Same flight across sources: target + flight numbers + scheduled departure."""
        return f"{self.target_id}|{'+'.join(self.flight_numbers)}|{self.depart:%Y-%m-%dT%H:%M}"

    @property
    def baggage_verified(self) -> bool:
        checked_ok = self.checked_included is True or self.checked_bag_inr is not None
        return checked_ok and self.cabin_included is True

    @property
    def comparable_inr(self) -> Decimal | None:
        """Baggage-adjusted price, or None when the checked-bag cost is not verifiable."""
        if not self.baggage_verified:
            return None
        return self.displayed_inr + (Decimal(0) if self.checked_included else (self.checked_bag_inr or Decimal(0)))

    @property
    def verification_state(self) -> str:
        if not self.fare_verified:
            return "discovery_only"  # search-level price, seller price not confirmed this run
        if not self.baggage_verified:
            return "baggage_cost_unknown"
        return "verified_total" if self.checked_included else "verified_but_baggage_extra"

    @property
    def alert_kind(self) -> str:
        """'deal' = verified all-inclusive price; 'lead' = promising but incomplete."""
        return "deal" if self.fare_verified and self.comparable_inr is not None else "lead"

    @property
    def alert_price(self) -> Decimal:
        c = self.comparable_inr
        return c if c is not None and self.fare_verified else self.displayed_inr


def eligible(fare: Fare, target: Target) -> bool:
    d = fare.depart.astimezone(IST)
    if (fare.origin, fare.dest, d.date()) != (target.origin, target.dest, target.date):
        return False
    return target.depart_after is None or d.time() > target.depart_after


def approved_url(url: str | None) -> bool:
    if not url:
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == d or host.endswith("." + d) for d in APPROVED_DOMAINS)


_MONEY = r"(?:₹|INR|Rs\.?)\s*([\d,]+(?:\.\d+)?)"


def parse_baggage(texts: list[str]) -> dict:
    """Read only explicit statements; anything not stated stays unknown."""
    out: dict = {"checked_included": None, "cabin_included": None, "checked_bag_inr": None}
    for t in texts:
        s = t.lower()
        if re.search(r"(free|included).*(carry-on|cabin)|(carry-on|cabin).*(free|included)", s):
            out["cabin_included"] = True
        elif re.search(r"no (free )?(carry-on|cabin)|(carry-on|cabin).*(not included|for a fee)", s):
            out["cabin_included"] = False
        if re.search(r"(free|included).*checked|checked.*(free|included)", s) and "not included" not in s:
            out["checked_included"] = True
        elif "checked" in s and (m := re.search(_MONEY, t)):
            out["checked_bag_inr"] = Decimal(m.group(1).replace(",", ""))
            out["checked_included"] = False
        elif re.search(r"checked.*(not included|for a fee)|no free checked", s):
            out["checked_included"] = False
    return out


def decide(state: dict | None, price: Decimal) -> tuple[bool, dict]:
    """Alert on first sub-threshold price, on every genuine new lower price, and on re-crossing.

    state: {'last_alerted': Decimal|None, 'below': bool} for one (flight, kind).
    """
    last = state["last_alerted"] if state else None
    was_below = state["below"] if state else False
    if price >= THRESHOLD_INR:
        return False, {"last_alerted": last, "below": False}
    fire = last is None or not was_below or price <= last - MIN_DROP_INR
    return fire, {"last_alerted": price if fire else last, "below": True}
