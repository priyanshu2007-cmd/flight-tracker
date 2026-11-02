"""Alert emails via the Gmail API (OAuth, gmail.send + gmail.metadata) with a persistent outbox."""
import base64
import os
import re
import smtplib
import time
from email.message import EmailMessage

from .config import ALERT_TO, IST, THRESHOLD_INR
from .db import ist, now

SCOPES = ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.metadata"]
TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "secrets/gmail_token.json")
CLIENT_FILE = os.environ.get("GMAIL_CLIENT_FILE", "secrets/gmail_client.json")
MAX_ATTEMPTS = 6
API = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


def inr(v) -> str:
    return "unknown" if v is None else f"₹{int(v):,}"


def render(f, kind: str, hist: dict) -> tuple[str, str]:
    route = f"{f.origin} → {f.dest}"
    day = f"{f.depart:%d %b %Y}"
    price = f.alert_price
    if kind == "deal":
        subject = f"Flight Price Drop: {route} | {inr(price)} | {day}"
    else:
        subject = f"UNVERIFIED LEAD (baggage/fare not confirmed): {route} | {inr(price)} | {day}"
    prev = hist.get("prev")
    if prev is None:
        reason = f"First observed price below the {inr(THRESHOLD_INR)} threshold."
    elif prev >= THRESHOLD_INR:
        reason = f"Price fell from {inr(prev)} to below the {inr(THRESHOLD_INR)} threshold."
    else:
        reason = f"New lower price: {inr(prev)} → {inr(price)}."
    low, low_at = hist.get("lowest"), hist.get("lowest_at")
    if low is None or price <= low:
        low, low_at = price, f.observed_at.astimezone(IST).strftime("%d %b %Y %H:%M IST")
    else:
        low_at = ist(low_at)
    bag = ("checked bag included" if f.checked_included else
           f"checked bag extra {inr(f.checked_bag_inr)}" if f.checked_bag_inr is not None else "checked-bag cost UNKNOWN")
    cabin = {True: "cabin bag included", False: "cabin bag NOT included", None: "cabin bag UNKNOWN"}[f.cabin_included]
    lines = [
        reason, "",
        f"Route: {route} (one way, 1 adult)",
        f"Date: {day}",
        f"Airline / flight: {f.airline or 'unknown'} {' + '.join(f.flight_numbers)}",
        f"Departs: {f.depart:%d %b %H:%M} IST   Arrives: {f.arrive and f.arrive.strftime('%d %b %H:%M') or 'unknown'} IST",
        f"Stops: {f.stops} ({'nonstop' if f.stops == 0 else 'connecting'})   Duration: "
        f"{f'{f.duration_min // 60}h {f.duration_min % 60}m' if f.duration_min else 'unknown'}",
        "",
        f"Current {'baggage-adjusted total' if kind == 'deal' else 'displayed fare'}: {inr(price)}",
        f"Displayed ticket fare: {inr(f.displayed_inr)}",
        f"Previous observed: {inr(prev)}",
        f"Lowest observed: {inr(low)} at {low_at}",
        f"Threshold: below {inr(THRESHOLD_INR)} per one-way ticket",
        f"Baggage: {bag}; {cabin}",
        *(f"  - {n}" for n in f.baggage_notes),
        f"Verification state: {f.verification_state}",
        f"Fare verified with seller this run: {'yes' if f.fare_verified else 'NO'}",
        f"Comparable all-inclusive price: {'yes' if kind == 'deal' else 'NO - check baggage before booking'}",
        "",
        f"Source: {f.source}{f' (seller: {f.seller})' if f.seller else ''}",
        f"Last verified: {f.observed_at.astimezone(IST):%d %b %Y %H:%M} IST",
        f"Book / check: {f.booking_url}",
        "",
        "Prices and availability can change before you book. Confirm the final price and baggage on the booking page.",
        "This tracker never books or pays for anything.",
    ]
    return subject, "\n".join(lines)


class GmailSender:
    def __init__(self):
        from google.auth.transport.requests import AuthorizedSession, Request
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if not creds.valid:
            creds.refresh(Request())
            fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_TRUNC | os.O_CREAT, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(creds.to_json())
        self.session = AuthorizedSession(creds)

    def send(self, subject: str, body: str) -> dict:
        msg = EmailMessage()
        msg["To"] = msg["From"] = ALERT_TO
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        err = ""
        for attempt in range(3):
            try:
                r = self.session.post(f"{API}/send", json={"raw": raw}, timeout=30)
            except Exception as e:  # network errors from requests/google-auth
                err, transient = f"network: {type(e).__name__}", True
            else:
                if r.status_code == 200:
                    mid = r.json()["id"]
                    return {"status": self._delivery(mid), "id": mid, "error": None}
                err, transient = f"HTTP {r.status_code}: {r.text[:200]}", r.status_code in (429, 500, 502, 503, 504)
            if not transient:
                break
            time.sleep(2 ** attempt)
        return {"status": "failed", "id": None, "error": err}

    def _delivery(self, mid: str) -> str:
        """Self-addressed mail lands in INBOX when delivered; otherwise Gmail only confirms acceptance."""
        time.sleep(3)
        try:
            r = self.session.get(f"{API}/{mid}", params={"format": "minimal"}, timeout=30)
            return "delivered" if r.ok and "INBOX" in r.json().get("labelIds", []) else "accepted"
        except Exception:
            return "accepted"


class SmtpSender:
    """Gmail SMTP with an app password (does not expire like a Testing-mode OAuth token).

    SMTP only confirms Gmail accepted the message, so the best status here is 'accepted', never 'delivered'.
    """

    def __init__(self, password: str):
        self.password = password

    def send(self, subject: str, body: str) -> dict:
        msg = EmailMessage()
        msg["To"] = msg["From"] = ALERT_TO
        msg["Subject"] = subject
        msg.set_content(body)
        err = ""
        for attempt in range(3):
            try:
                with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
                    s.login(ALERT_TO, self.password)
                    s.send_message(msg)
                return {"status": "accepted", "id": msg["Message-ID"], "error": None}
            except smtplib.SMTPAuthenticationError as e:
                return {"status": "failed", "id": None, "error": f"smtp auth rejected ({e.smtp_code})"}  # no retry
            except (smtplib.SMTPException, OSError) as e:
                err = f"smtp: {type(e).__name__}"
            time.sleep(2 ** attempt)
        return {"status": "failed", "id": None, "error": err}


class ChainSender:
    """Try each sender in order; a failed one (bad password, outage, missing token) falls through to the next."""

    def __init__(self, *makers):
        self.makers = makers

    def send(self, subject: str, body: str) -> dict:
        res = {"status": "failed", "id": None, "error": "no sender configured"}
        for make in self.makers:
            try:
                res = make().send(subject, body)
            except Exception as e:  # e.g. missing OAuth token file
                res = {"status": "failed", "id": None, "error": f"{type(e).__name__}"}
            if res["status"] != "failed":
                return res
        return res


def default_sender():
    """App password first (long-lived), with the OAuth token as fallback. A malformed secret is ignored."""
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not pw:
        return GmailSender()
    if not re.fullmatch(r"[a-z]{16}", pw):  # Google app passwords are exactly 16 lowercase letters
        print("warning: GMAIL_APP_PASSWORD is not a 16-letter app password; ignoring it", flush=True)
        return GmailSender()
    return ChainSender(lambda: SmtpSender(pw), GmailSender)


def queue(con, idem_key: str, flight_key: str, kind: str, price, prev, subject: str, body: str) -> bool:
    """Insert an alert once; returns False if this exact event was already queued (idempotent)."""
    cur = con.execute("INSERT OR IGNORE INTO alerts (idem_key,flight_key,kind,price,prev_price,subject,body,status,"
                      "created_at) VALUES (?,?,?,?,?,?,?,'pending',?)",
                      (idem_key, flight_key, kind, str(price), prev and str(prev), subject, body, now()))
    return cur.rowcount == 1


def flush(con, sender=None, dry_run: bool = False) -> list[dict]:
    """Send every pending/failed alert. Dry-run never contacts Gmail."""
    rows = con.execute("SELECT id, subject, body FROM alerts WHERE status IN ('pending','failed') AND attempts < ?"
                       " ORDER BY id", (MAX_ATTEMPTS,)).fetchall()
    results = []
    for r in rows:
        if dry_run:
            res = {"status": "dry-run", "id": None, "error": None}
        else:
            try:
                sender = sender or default_sender()
                res = sender.send(r["subject"], r["body"])
            except Exception as e:  # missing/expired token: keep the alert pending for the next run
                res = {"status": "failed", "id": None, "error": f"auth: {type(e).__name__}: {str(e)[:150]}"}
        con.execute("UPDATE alerts SET status=?, provider_id=?, error=?, attempts=attempts+1, sent_at=? WHERE id=?",
                    (res["status"], res["id"], res["error"], now() if res["status"] in ("accepted", "delivered") else None,
                     r["id"]))
        results.append({"alert": r["id"], **res})
    return results
