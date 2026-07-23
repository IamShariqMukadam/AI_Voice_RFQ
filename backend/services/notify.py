"""
Delivers a finished lead. Tries, in order: the existing WordPress form's
own endpoint (so the client's current email templates / CRM hooks keep
firing unchanged) -> direct SMTP -> a local JSONL file. Always returns
cleanly - a demo or a misconfigured .env should never crash a request.
"""
import json
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import config

logger = logging.getLogger("notify")

# Same display labels the frontend uses - keeps the email readable instead
# of showing raw internal values like "cooling_electric_heat" or "2_ton".
CATEGORY_LABELS = {
    "heating": "Heating",
    "cooling_electric_heat": "Cooling, Electric Heat",
    "cooling_heat_pump": "Cooling, Heat Pump",
}
TONNAGE_LABELS = {"2_ton": "2 Ton", "2.5_ton": "2.5 Ton", "3_ton": "3 Ton", "3.5_ton": "3.5 Ton", "4_ton": "4 Ton"}
LOCATION_LABELS = {"attic_horizontal": "Attic (Horizontal)", "closet_vertical": "Closet (Vertical)", "garage_vertical": "Garage"}
ACTION_LABELS = {"go_with_plan": "Go With This Plan", "arrange_call": "Arrange A Call", "arrange_visit": "Arrange A Visit"}


def _plan_name(session) -> str:
    pid = session.slots.get("plan_choice")
    ids = pid if isinstance(pid, list) else ([pid] if pid else [])
    names = []
    for i in ids:
        plan = next((p for p in session.available_plans if p.get("id") == i), None)
        names.append(plan["name"] if plan else str(i))
    return " + ".join(names)


def _rows(session):
    s = session.slots
    address = ", ".join(x for x in (s.get("street"), s.get("city"), s.get("zip")) if x)
    rows = [
        ("Name", s.get("full_name", "")),
        ("Phone", s.get("phone", "")),
        ("Email", s.get("email", "")),
        ("Address", address),
        ("System Type", CATEGORY_LABELS.get(s.get("category"), s.get("category", ""))),
        ("Tonnage", TONNAGE_LABELS.get(s.get("tonnage"), s.get("tonnage", ""))),
        ("Air Handler Location", LOCATION_LABELS.get(s.get("location"), s.get("location", ""))),
        ("Plan", _plan_name(session)),
        ("Requested Action", ACTION_LABELS.get(s.get("plan_action"), s.get("plan_action", ""))),
    ]
    if s.get("appointment_date"):
        kind = "Visit" if s.get("appointment_type") == "visit" else "Callback"
        rows.append((f"Scheduled {kind}", f"{s['appointment_date']} at {s.get('appointment_time', '')}"))
    if s.get("call_timing") == "immediate":
        rows.append(("Callback Timing", "IMMEDIATE - call right now"))
    return rows


def _format_lead_text(session) -> str:
    lines = ["New AC / Heating quote request (voice assistant)", ""]
    lines += [f"{label}: {value}" for label, value in _rows(session)]
    return "\n".join(lines)


def _format_lead_html(session) -> str:
    rows_html = "".join(
        f'<tr><td style="padding:6px 12px;border:1px solid #ddd;background:#f5f5f5;font-weight:600;">{label}</td>'
        f'<td style="padding:6px 12px;border:1px solid #ddd;">{value or "-"}</td></tr>'
        for label, value in _rows(session)
    )
    return (
        '<div style="font-family:Arial,sans-serif;">'
        "<h2>New AC / Heating Quote Request</h2>"
        '<table style="border-collapse:collapse;">' + rows_html + "</table></div>"
    )


def _action_followup_text(session) -> str:
    return {
        "go_with_plan": "confirm your plan and get things scheduled",
        "arrange_call": "give you a call",
        "arrange_visit": "arrange your visit",
    }.get(session.slots.get("plan_action"), "follow up with you")


def _format_customer_text(session) -> str:
    first_name = (session.slots.get("full_name", "") or "").split(" ", 1)[0] or "there"
    lines = [
        f"Hi {first_name},",
        "",
        "Thanks for requesting an instant quote from Polar Express AC & Heating! "
        f"We've got your request and our team will reach out shortly to {_action_followup_text(session)}.",
        "",
        "Here's what you submitted, so you can double check it:",
        "",
    ]
    lines += [f"{label}: {value}" for label, value in _rows(session)]
    lines += ["", "If anything above isn't quite right, just reply to this email and let us know.", "", "Thanks,", "Polar Express AC & Heating"]
    return "\n".join(lines)


def _format_customer_html(session) -> str:
    first_name = (session.slots.get("full_name", "") or "").split(" ", 1)[0] or "there"
    rows_html = "".join(
        f'<tr><td style="padding:6px 12px;border:1px solid #ddd;background:#f5f5f5;font-weight:600;">{label}</td>'
        f'<td style="padding:6px 12px;border:1px solid #ddd;">{value or "-"}</td></tr>'
        for label, value in _rows(session)
    )
    return (
        '<div style="font-family:Arial,sans-serif;">'
        f"<h2>Thanks, {first_name}!</h2>"
        f"<p>We've received your instant quote request from Polar Express AC &amp; Heating. "
        f"Our team will reach out shortly to {_action_followup_text(session)}.</p>"
        "<p>Here's what you submitted, so you can double check it:</p>"
        f'<table style="border-collapse:collapse;">{rows_html}</table>'
        "<p>If anything above isn't quite right, just reply to this email and let us know.</p></div>"
    )


def _send_customer_confirmation(server, session) -> None:
    """Best-effort - a failure here should never undo the org lead email
    that already succeeded, so this is called from inside its own
    try/except and never affects submit_lead's return value."""
    to_addr = (session.slots.get("email") or "").strip()
    if not to_addr:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Your Polar Express AC & Heating Quote Request"
        msg["From"] = config.NOTIFY_EMAIL_FROM
        msg["To"] = to_addr
        msg.attach(MIMEText(_format_customer_text(session), "plain"))
        msg.attach(MIMEText(_format_customer_html(session), "html"))
        server.sendmail(config.NOTIFY_EMAIL_FROM, [to_addr], msg.as_string())
        logger.info("[%s] confirmation emailed to customer", session.session_id)
    except Exception as exc:
        logger.warning("[%s] customer confirmation email failed: %s", session.session_id, exc)


class _PartialSession:
    def __init__(self, session_id: str, slots: dict, available_plans=None):
        self.session_id = session_id
        self.slots = slots
        self.available_plans = available_plans or []


def submit_partial_lead(session_id: str, slots: dict, available_plans=None, stage: str = "") -> bool:
    """Deliver a recoverable lead that did not reach the final submit step."""
    session = _PartialSession(session_id, slots, available_plans)
    payload = {
        **slots,
        "session_id": session_id,
        "stage": stage,
        "status": "incomplete_abandoned",
        "note": "Customer started the voice quote but did not finish. Follow up to recover the lead.",
    }

    if config.WP_SUBMIT_ENDPOINT:
        try:
            import httpx
            r = httpx.post(config.WP_SUBMIT_ENDPOINT, json=payload, timeout=10)
            r.raise_for_status()
            logger.info("[%s] partial lead submitted to WP endpoint", session_id)
            return True
        except Exception as exc:
            logger.warning("[%s] WP endpoint partial submit failed: %s", session_id, exc)

    if config.SMTP_HOST and config.NOTIFY_EMAIL_TO:
        try:
            subject = "Incomplete Quote Request - Follow Up Needed"
            banner_text = (
                "CUSTOMER DID NOT FINISH THIS REQUEST.\n"
                "They gave contact information, so please follow up to complete the quote.\n\n"
            )
            banner_html = (
                '<p style="color:#b45309;font-weight:bold;">'
                "Customer did not finish this request. Follow up to complete the quote."
                "</p>"
            )
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = config.NOTIFY_EMAIL_FROM
            msg["To"] = config.NOTIFY_EMAIL_TO
            msg.attach(MIMEText(banner_text + _format_lead_text(session), "plain"))
            msg.attach(MIMEText(banner_html + _format_lead_html(session), "html"))
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as server:
                server.starttls()
                if config.SMTP_USER:
                    server.login(config.SMTP_USER, config.SMTP_PASS)
                server.sendmail(config.NOTIFY_EMAIL_FROM, [config.NOTIFY_EMAIL_TO], msg.as_string())
            logger.info("[%s] partial lead emailed", session_id)
            return True
        except Exception as exc:
            logger.warning("[%s] SMTP partial send failed: %s", session_id, exc)

    try:
        with open(config.LEADS_FALLBACK_FILE, "a") as f:
            f.write(json.dumps(payload) + "\n")
        logger.info("[%s] partial lead saved to fallback file (%s)", session_id, config.LEADS_FALLBACK_FILE)
        return True
    except Exception as exc:
        logger.error("[%s] partial fallback save failed: %s", session_id, exc)
    return False


def submit_lead(session, urgent: bool = False) -> bool:
    payload = {**session.slots, "session_id": session.session_id}

    if config.WP_SUBMIT_ENDPOINT:
        try:
            import httpx
            r = httpx.post(config.WP_SUBMIT_ENDPOINT, json=payload, timeout=10)
            r.raise_for_status()
            logger.info("[%s] lead submitted to WP endpoint", session.session_id)
            return True
        except Exception as exc:
            logger.warning("[%s] WP endpoint submit failed: %s", session.session_id, exc)

    if config.SMTP_HOST and config.NOTIFY_EMAIL_TO:
        try:
            msg = MIMEMultipart("alternative")
            subject = "New Instant Quote Request (Voice Assistant)"
            banner_text, banner_html = "", ""
            if urgent:
                subject = "URGENT - Customer wants an IMMEDIATE callback"
                banner_text = "CALL THIS CUSTOMER RIGHT NOW - they asked for an immediate callback, not a scheduled one.\n\n"
                banner_html = '<p style="color:#b91c1c;font-weight:bold;">CALL THIS CUSTOMER RIGHT NOW - immediate callback requested, not scheduled.</p>'
            msg["Subject"] = subject
            msg["From"] = config.NOTIFY_EMAIL_FROM
            msg["To"] = config.NOTIFY_EMAIL_TO
            # Plain-text part first, HTML second - per MIME convention the
            # LAST alternative is the one most clients render by default.
            msg.attach(MIMEText(banner_text + _format_lead_text(session), "plain"))
            msg.attach(MIMEText(banner_html + _format_lead_html(session), "html"))
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as server:
                server.starttls()
                if config.SMTP_USER:
                    server.login(config.SMTP_USER, config.SMTP_PASS)
                server.sendmail(config.NOTIFY_EMAIL_FROM, [config.NOTIFY_EMAIL_TO], msg.as_string())
                _send_customer_confirmation(server, session)
            logger.info("[%s] lead emailed%s", session.session_id, " (URGENT)" if urgent else "")
            return True
        except Exception as exc:
            logger.warning("[%s] SMTP send failed: %s", session.session_id, exc)

    try:
        with open(config.LEADS_FALLBACK_FILE, "a") as f:
            f.write(json.dumps({**payload, "urgent": urgent}) + "\n")
        logger.info("[%s] lead saved to fallback file (%s)", session.session_id, config.LEADS_FALLBACK_FILE)
    except Exception as exc:
        logger.error("[%s] fallback save failed: %s", session.session_id, exc)
    return False