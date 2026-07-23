"""
Optional Google Calendar event creation for booked call slots. Wrapped
so a missing/misconfigured service account NEVER breaks lead
submission or slot booking - same graceful-degradation pattern as
notify.py's WP-endpoint -> SMTP -> JSONL fallback chain. If this
returns None, the booking still saved to booked_slots and the lead
still got emailed either way; only the calendar invite is skipped.

IMPORTANT - inviting the customer as an attendee (attendees=[...] with
sendUpdates="all") is a Google API call a bare service account is NOT
allowed to make: "Service accounts cannot invite attendees without
Domain-Wide Delegation of Authority". Without delegation, that call
used to fail every time, so the customer never got the calendar email
the closing script promised, even though nothing here raised or
crashed - it just logged and returned None.

One-time setup (a Google Workspace admin task, not code):
1. Create a Google Cloud service account, download its JSON key file.
2. Set GOOGLE_SERVICE_ACCOUNT_FILE in .env to that key file's path.
3. Set GOOGLE_CALENDAR_ID in .env (defaults to 'primary').
4. pip install google-api-python-client google-auth (in requirements.txt).
5. To actually email the customer an invite, you additionally need
   domain-wide delegation: in Google Workspace Admin ->
   Security -> API controls -> Domain-wide delegation, authorize this
   service account's Client ID for scope
   https://www.googleapis.com/auth/calendar, then set
   GOOGLE_DELEGATED_USER in .env to a real Workspace mailbox (e.g.
   scheduling@yourcompany.com) that the service account impersonates
   to send the invite. Without step 5, the event is still created
   internally (share the calendar with the service account so it's
   visible - Calendar Settings > Share with specific people), but the
   customer is NOT emailed - see attendee_invited in the return value.

Without steps 1-4 done at all, create_call_event() just logs and
returns None.
"""
import json
import logging
from datetime import datetime, timedelta

import config

logger = logging.getLogger("calendar_service")

_warned_no_delegation = False  # log the delegation-setup hint once, not on every call


def create_call_event(session, call_date: str, call_time: str) -> dict | None:
    """Returns None if the calendar isn't configured or the API call
    failed outright. Otherwise returns {"link": <event url or None>,
    "attendee_invited": bool} - callers (e.g. the closing script) must
    check attendee_invited before promising the customer a calendar
    email, since a successful event can still mean the invite wasn't
    sent (no domain-wide delegation configured)."""
    global _warned_no_delegation
    if not config.GOOGLE_SERVICE_ACCOUNT_FILE and not config.GOOGLE_SERVICE_ACCOUNT_JSON:
        logger.info("[%s] Google Calendar not configured - skipping event creation", session.session_id)
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        logger.warning("google-api-python-client not installed - skipping calendar event")
        return None

    try:
        scopes = ["https://www.googleapis.com/auth/calendar"]
        if config.GOOGLE_SERVICE_ACCOUNT_JSON:
            creds = service_account.Credentials.from_service_account_info(
                json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON), scopes=scopes
            )
        else:
            creds = service_account.Credentials.from_service_account_file(
                config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes
            )
        can_invite_attendees = bool(config.GOOGLE_DELEGATED_USER)
        if can_invite_attendees:
            # Impersonate a real Workspace mailbox via domain-wide
            # delegation - this is the only way a service account is
            # allowed to add attendees and trigger invite emails.
            creds = creds.with_subject(config.GOOGLE_DELEGATED_USER)
        elif not _warned_no_delegation:
            logger.warning(
                "GOOGLE_DELEGATED_USER not set - calendar events will be created but the "
                "customer will NOT receive an invite email (Google blocks service accounts "
                "from inviting attendees without domain-wide delegation). See "
                "calendar_service.py module docstring step 5 to enable customer invites."
            )
            _warned_no_delegation = True

        service = build("calendar", "v3", credentials=creds)
        start_dt = datetime.strptime(f"{call_date} {call_time}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(minutes=30)
        name = session.slots.get("full_name", "Customer")
        appt_type = session.slots.get("appointment_type", "call")
        summary_kind = "Site Visit" if appt_type == "visit" else "Callback"
        plan_names = ", ".join(
            p["name"] for p in session.available_plans if p["id"] in _plan_ids(session)
        ) or "N/A"

        event = {
            "summary": f"Quote {summary_kind} - {name}",
            "description": (
                f"Phone: {session.slots.get('phone', '')}\n"
                f"Address: {session.slots.get('street', '')}, {session.slots.get('city', '')} {session.slots.get('zip', '')}\n"
                f"Plan(s) discussed: {plan_names}\n"
                f"Booked via S2S voice assistant, session {session.session_id}."
            ),
            "start": {"dateTime": start_dt.isoformat(), "timeZone": config.CALENDAR_TIMEZONE},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": config.CALENDAR_TIMEZONE},
            "reminders": {"useDefault": True},
        }
        email = session.slots.get("email")
        attendee_invited = False
        send_updates = "none"
        if email and can_invite_attendees:
            event["attendees"] = [{"email": email}]
            send_updates = "all"  # emails both the delegated organizer and the customer
            attendee_invited = True

        created = service.events().insert(
            calendarId=config.GOOGLE_CALENDAR_ID, body=event, sendUpdates=send_updates
        ).execute()
        return {"link": created.get("htmlLink"), "attendee_invited": attendee_invited}
    except Exception:
        logger.exception("[%s] Google Calendar event creation failed", session.session_id)
        return None


def _plan_ids(session):
    pid = session.slots.get("plan_choice")
    return pid if isinstance(pid, list) else ([pid] if pid else [])