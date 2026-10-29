"""FlightPowers snapshot for emails. Display only: never feeds observations, thresholds or alerts.

The cloud job cannot call the FlightPowers connector (it needs the owner's signed-in session), so the
results are saved in snapshots/flightpowers.json and shown with their capture time. Refresh by re-running
the searches in Claude and re-saving the file.
"""
import json
import pathlib

from .config import TARGETS
from .core import approved_url

SNAPSHOT = pathlib.Path(__file__).resolve().parent.parent / "snapshots" / "flightpowers.json"


def _link(tfs: str) -> str | None:
    url = f"https://www.google.com/travel/flights?tfs={tfs}&curr=inr"
    return url if approved_url(url) else None  # approved-domain rule applies to every link we email


def section(path: pathlib.Path = SNAPSHOT) -> str:
    try:
        snap = json.loads(path.read_text())
    except (OSError, ValueError):
        return ""  # no snapshot: status email goes out without the section
    out = ["FLIGHTPOWERS FARES (Google Flights, via connector)",
           f"Captured: {snap['captured']}. NOT refreshed automatically: fares go stale within minutes, so",
           f"treat these as a reference and confirm the price on the booking page. {snap['usage']}.", ""]
    for t in TARGETS:
        d = snap["targets"].get(t.id)
        if not d:
            continue
        rule = " (departures after 17:00 only)" if t.depart_after else ""
        out.append(f"Target {t.id}: {t.origin} → {t.dest}, {t.date:%d %b %Y}{rule}")
        ins = d.get("insights")
        if ins:
            out.append(f"  Google's typical range: ₹{ins['low']:,} to ₹{ins['high']:,}; current prices are '{ins['level']}' for this period")
        for i, (airline, dep, stops, dur, price, tfs) in enumerate(sorted(d["rows"], key=lambda r: r[4]), 1):
            kind = "nonstop" if stops == 0 else f"{stops} stop"
            out.append(f"  {i:>2}. ₹{price:,}  {airline}, departs {dep}, {kind}, {dur}")
            link = _link(tfs)
            if link:
                out.append(f"      {link}")
        out.append("")
    out.append("Baggage rules and mandatory fees are NOT included in this data, so none of these is a verified all-inclusive price.")
    return "\n".join(out)
