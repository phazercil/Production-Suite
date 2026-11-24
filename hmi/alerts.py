"""Downtime alerts by email and text message (Operator Kiosk > Admin > Alerts).

When a line has been stopped for a person's "down after" minutes (planned breaks, lunch and start-up time
don't count), they get one message for that stop, and optionally one more when the line runs again.
The same people can also be told when a QC check is overdue by their "QC late" minutes, when a QC check fails
and waits for a supervisor, and when planned maintenance falls due (Maintenance › PM schedule). Each is one
message per check, stop or request.

Email goes through your mail server (SMTP). It must be on the plant network unless "the mail server is on the
internet" is ticked. Text messages go through Twilio, a cloud service, so like Paycom they are an internet
connection, used only when a Twilio account is set up here. A text can also be sent without Twilio by using the
carrier's email-to-text address as the person's email.
"""
import base64
import json
import logging
import re
import smtplib
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from email.message import EmailMessage

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from plant import liveconfig, netsafety

from .models import AlertContact, AlertLog, ShiftRun

log = logging.getLogger("plant")
EMAIL_RE = re.compile(r"^[^@\s\"<>,;]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,}$")


class AlertError(Exception):
    pass


def phone_e164(v):
    """'(555) 123-4567' -> '+15551234567'. Returns '' for blanks; raises for numbers that can't be right."""
    v = str(v or "").strip()
    if not v:
        return ""
    digits = re.sub(r"\D", "", v)
    if v.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    raise AlertError(f"{v} doesn't look like a phone number. Use 10 digits, or +country code and number.")


def smtp_ready():
    liveconfig.refresh()
    return bool(settings.SUITE.get("ALERT_SMTP_HOST") and settings.SUITE.get("ALERT_FROM"))


def sms_ready():
    liveconfig.refresh()
    s = settings.SUITE
    return bool(s.get("TWILIO_SID") and s.get("TWILIO_TOKEN") and s.get("TWILIO_FROM"))


def send_email(to, subject, body):
    s = settings.SUITE
    host, port = s.get("ALERT_SMTP_HOST"), int(s.get("ALERT_SMTP_PORT") or 25)
    if not host:
        raise AlertError("No mail server is set up (Admin > Alerts).")
    if str(s.get("ALERT_SMTP_INTERNET") or "") != "1":
        try:
            netsafety.check_host(host, "The mail server")
        except netsafety.NotPrivate as e:
            raise AlertError(f"{e} Tick \"the mail server is on the internet\" to allow it.")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.get("ALERT_FROM"), to, subject
    msg.set_content(body)
    sec = (s.get("ALERT_SMTP_SECURITY") or "starttls").lower()
    try:
        if sec == "ssl":
            srv = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
        else:
            srv = smtplib.SMTP(host, port, timeout=15)
        with srv:
            if sec == "starttls":
                srv.starttls(context=ssl.create_default_context())
            if s.get("ALERT_SMTP_USER"):
                srv.login(s.get("ALERT_SMTP_USER"), s.get("ALERT_SMTP_PASSWORD") or "")
            srv.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise AlertError(f"The mail server didn't take the message: {e}")


def send_sms(to, body):
    s = settings.SUITE
    if not sms_ready():
        raise AlertError("Text messages are not set up (Admin > Alerts > Twilio).")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(s['TWILIO_SID'])}/Messages.json"
    data = urllib.parse.urlencode({"To": to, "From": s["TWILIO_FROM"], "Body": body[:600]}).encode()
    auth = base64.b64encode(f"{s['TWILIO_SID']}:{s['TWILIO_TOKEN']}".encode()).decode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Authorization": "Basic " + auth,
                                                                         "Content-Type": "application/x-www-form-urlencoded"})
    from .integrations import _PUBLIC_OPENER  # Twilio is a cloud service: the only route that may leave the plant network
    try:
        with _PUBLIC_OPENER.open(req, timeout=15) as r:
            json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail, code = "", None
        try:
            body = json.loads(e.read().decode())
            detail, code = body.get("message", ""), body.get("code")
        except Exception:
            pass
        if code in (572006, 21608) or "trial account" in detail.lower():
            raise AlertError("Twilio refused the text because the Twilio account is still a free trial. Trial accounts can only send "
                             "Twilio's own sample messages (and only to numbers verified in Twilio), not the suite's alerts. "
                             "Upgrade the account in the Twilio Console (Billing › Upgrade), then send the test again.")
        raise AlertError(f"Twilio refused the text ({e.code}{': ' + detail if detail else ''}).")
    except (urllib.error.URLError, OSError) as e:
        raise AlertError(f"Couldn't reach Twilio: {getattr(e, 'reason', e)}")


def deliver(contact_name, email, phone, subject, body, kind, line="", stop_key=""):
    """Send by every channel the person has; log each result. Never raises."""
    results = []
    for channel, to in (("email", email), ("sms", phone)):
        if not to:
            continue
        try:
            if channel == "email":
                send_email(to, subject, body)
            else:
                send_sms(to, f"{subject}. {body}" if kind != "test" else body)
            ok, msg = True, "sent"
        except AlertError as e:
            ok, msg = False, str(e)
        except Exception as e:  # never let a message problem stop the ticker
            ok, msg = False, f"{type(e).__name__}: {e}"
        AlertLog.objects.create(t=timezone.now(), kind=kind, line=line, stop_key=stop_key, contact=contact_name, channel=channel,
                                to=to[:200], ok=ok, msg=msg[:300])
        results.append({"channel": channel, "to": to, "ok": ok, "msg": msg})
    return results


def _down_minutes(sh, now):
    from .engine import minus_planned, planned_windows
    if not sh.stop_start:
        return 0
    return sum((e - s).total_seconds() for s, e in minus_planned(sh.stop_start, now, planned_windows(sh))) / 60


_busy = threading.Lock()


def _told(key, contact, kind):
    return AlertLog.objects.filter(stop_key=key, contact=contact, kind=kind).exists()


def _for(c, line):
    return not c.lines or not line or line in c.lines


def quality_jobs(now, contacts, running):
    """QC checks overdue by each person's "QC late" minutes, and failed checks waiting for a supervisor.
    One message per check per person. The kiosk shows the same check as due / late on the line's own screen."""
    from plant.models import PlantSettings

    from . import qc
    from .models import QCCheck
    jobs = []
    late_people = [c for c in contacts if c.qc_late and c.qc_late > 0]
    if late_people:
        st = PlantSettings.load()
        for line, sh in running.items():
            rc = qc.recheck_for(line)
            due = qc.due_at(sh, st.settings, now, rc)
            if not due:
                continue
            over = (now - due).total_seconds() / 60
            key = f"qc|{line}|{due.isoformat()}"
            what = "QC re-check" if rc else "QC check"
            for c in late_people:
                if _for(c, line) and over >= c.qc_late and not _told(key, c.name, "qclate"):
                    body = (f"The {what.lower()} on {line} was due at {timezone.localtime(due):%I:%M %p} and is {round(over)} min overdue."
                            f"{' MO ' + sh.mo.number + '.' if sh.mo else ''} Operator: {sh.user_name or 'not logged in'}.")
                    jobs.append((c.name, c.email, c.phone, f"{what} overdue on {line}", body, "qclate", line, key))
    fail_people = [c for c in contacts if c.qc_fail]
    if fail_people:
        for q in QCCheck.objects.filter(passed=False, review="pending", t__gte=now - timedelta(hours=8)):
            key = f"qcfail|{q.pk}"
            for c in fail_people:
                if _for(c, q.line) and not _told(key, c.name, "qcfail"):
                    body = (f"A QC check on {q.line} at {timezone.localtime(q.t):%I:%M %p} by {q.by} is out of spec"
                            f"{' (' + ', '.join(q.fails) + ')' if q.fails else ''}. MO {q.mo or '—'}, part {q.item}. "
                            "It needs a supervisor to approve, disapprove or Process Out.")
                    jobs.append((c.name, c.email, c.phone, f"QC check failed on {q.line}", body, "qcfail", q.line, key))
    return jobs


def pm_jobs(now, contacts):
    """Planned maintenance that fell due (its request opened in the last day)."""
    from .models import Ticket
    people = [c for c in contacts if c.pm_due]
    if not people:
        return []
    jobs = []
    for t in Ticket.objects.filter(pm__isnull=False, t__gte=now - timedelta(days=1)).exclude(status="Closed"):
        key = f"pm|{t.pk}"
        for c in people:
            if _for(c, t.machine) and not _told(key, c.name, "pmdue"):
                jobs.append((c.name, c.email, c.phone, f"Planned maintenance due on {t.machine}", f"{t.type} on {t.machine} is due. "
                             "The request is on the Maintenance page.", "pmdue", t.machine, key))
    return jobs


def check(now=None):
    """Called by the ticker. Works out who should be told what, then sends in the background."""
    now = now or timezone.now()
    contacts = list(AlertContact.objects.filter(active=True))
    if not contacts:
        return
    jobs = []
    running = {sh.line: sh for sh in ShiftRun.objects.filter(active=True).select_related("mo")}
    for line, sh in running.items():
        if not sh.stop_start or sh.state != "stopped":
            continue
        mins = _down_minutes(sh, now)
        key = f"{line}|{sh.stop_start.isoformat()}"
        for c in contacts:
            if c.lines and line not in c.lines:
                continue
            if c.down_after > 0 and mins >= c.down_after and not AlertLog.objects.filter(stop_key=key, contact=c.name, kind="down").exists():
                subject = f"{line} is down"
                body = (f"{line} has been stopped for {round(mins)} min (since {timezone.localtime(sh.stop_start):%I:%M %p})."
                        f"{' MO ' + sh.mo.number + '.' if sh.mo else ''} Operator: {sh.user_name or 'not logged in'}.")
                jobs.append((c.name, c.email, c.phone, subject, body, "down", line, key))
    jobs += quality_jobs(now, contacts, running)
    jobs += pm_jobs(now, contacts)
    # back running: a stop someone was told about has ended
    since = now - timedelta(hours=24)
    told = AlertLog.objects.filter(kind="down", t__gte=since, ok=True).values("stop_key", "contact", "line").distinct()
    for r in told:
        sh = running.get(r["line"])
        if sh and sh.stop_start and f"{r['line']}|{sh.stop_start.isoformat()}" == r["stop_key"]:
            continue  # still down
        if AlertLog.objects.filter(stop_key=r["stop_key"], contact=r["contact"], kind="back").exists():
            continue
        if sh is None:   # the shift ended while it was down: it didn't run again, so there's nothing to tell
            AlertLog.objects.create(t=now, kind="back", line=r["line"], stop_key=r["stop_key"], contact=r["contact"], channel="none",
                                    to="", ok=True, msg="not sent (the shift ended without the line running again)")
            continue
        if sh.state != "running":
            continue     # the stop ended without the line running (changeover, no machine data): wait until it does
        c = next((x for x in contacts if x.name == r["contact"]), None)
        if not c or not c.notify_back:
            AlertLog.objects.create(t=now, kind="back", line=r["line"], stop_key=r["stop_key"], contact=r["contact"], channel="none",
                                    to="", ok=True, msg="not sent (turned off for this person)")
            continue
        jobs.append((c.name, c.email, c.phone, f"{r['line']} is running again", f"{r['line']} is back up as of {timezone.localtime(now):%I:%M %p}.",
                     "back", r["line"], r["stop_key"]))
    if not jobs or not _busy.acquire(blocking=False):
        return

    def run():
        try:
            for j in jobs:
                deliver(*j)
        finally:
            close_old_connections()
            _busy.release()
    threading.Thread(target=run, name="alerts", daemon=True).start()
