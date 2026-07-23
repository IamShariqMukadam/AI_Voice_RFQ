"""
"Call me right now" trigger for plan_action=arrange_call -> call_timing=
immediate. Two layers, same graceful-degradation pattern as
calendar_service.py:

1. ALWAYS: an urgent lead email/webhook (services/notify.py's
   submit_lead(urgent=True)) - guaranteed to fire, no external service
   needed. This is what actually gets the customer called back even if
   nothing below is configured.
2. OPTIONAL: if Twilio is configured (TWILIO_ACCOUNT_SID/AUTH_TOKEN/
   TWILIO_FROM_NUMBER/ORG_PHONE_NUMBER), places a real outbound phone
   call to the organization's number, reads out the customer's details,
   then dials the customer directly so the org can be bridged straight
   through - no separate manual dial needed on their end.

Untestable from this sandbox (no network access to api.twilio.com here)
- written against Twilio's documented REST API/TwiML shape. Verify
against a real trial account before relying on it; this needs its own
Twilio account + phone number purchase, which is a real cost/setup step
outside this codebase, not something the code can do for you.

Phone number format note: extraction.clean_phone() only ever returns a
bare 10-digit local number (no "+", no country code - see
services/extraction.py). Twilio's <Dial> needs full E.164 to route a
call, so _to_e164() below normalizes the customer's number before it's
dialed. Without this, the org leg (dialed from ORG_PHONE_NUMBER, which
the operator types into .env in full E.164 already) would connect fine,
while the customer leg would silently fail to route - easy to miss
because the first half of the call still appears to work.
"""
import logging

import config
from services import notify

logger = logging.getLogger("call_service")


def _to_e164(phone: str, default_country_code: str = "+91") -> str:
    """Normalizes a bare local number into E.164 so Twilio can route it.

    Pass-through, not a guess: already-E.164 input (leading '+') is
    returned untouched, so this stays safe even if clean_phone's contract
    ever changes to include one. Only a bare, prefix-less number gets the
    default country code prepended - defaults to "+91" for now (see
    call_service.trigger_immediate_call, which always passes
    config.DEFAULT_COUNTRY_CODE explicitly anyway; this parameter default
    only matters for a direct/standalone call to this helper).
    """
    phone = (phone or "").strip()
    if not phone or phone.startswith("+"):
        return phone
    return f"{default_country_code}{phone}"


def trigger_immediate_call(session) -> dict:
    urgent_sent = notify.submit_lead(session, urgent=True)
    result = {"urgent_notified": urgent_sent, "call_placed": False, "call_sid": None}

    if not (config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN and config.TWILIO_FROM_NUMBER and config.ORG_PHONE_NUMBER):
        logger.info("[%s] Twilio not configured - urgent email is the only immediate-call action", session.session_id)
        return result

    try:
        from twilio.rest import Client
    except ImportError:
        logger.warning("twilio package not installed - urgent email is the only immediate-call action")
        return result

    try:
        client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
        name = session.slots.get("full_name", "a customer")
        phone = session.slots.get("phone", "")
        dial_number = _to_e164(phone, config.DEFAULT_COUNTRY_CODE)
        # Say the customer's details to whoever picks up at the org, then
        # bridge straight into the customer's number - one call for the
        # team, no manual re-dial. <Dial> hangs up automatically once
        # either side ends the bridged call. The spoken line reads the
        # original local digits (not the E.164 version) so it doesn't
        # read the country code out loud as part of the phone number.
        twiml = (
            f'<Response><Say voice="alice">New lead requesting an immediate callback. '
            f'{_say_safe(name)}, phone number '
            f"{' '.join(phone)}. Connecting you now.</Say>"
            f'<Dial callerId="{config.TWILIO_FROM_NUMBER}">{dial_number}</Dial></Response>'
        )
        call = client.calls.create(to=config.ORG_PHONE_NUMBER, from_=config.TWILIO_FROM_NUMBER, twiml=twiml)
        result["call_placed"] = True
        result["call_sid"] = call.sid
        logger.info("[%s] Twilio call placed to org, sid=%s", session.session_id, call.sid)
    except Exception:
        logger.exception("[%s] Twilio call failed - urgent email already sent as the fallback", session.session_id)

    return result


def _say_safe(text: str) -> str:
    return (text or "").replace("<", "").replace(">", "").replace("&", "and")