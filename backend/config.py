import os
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------
# OpenAI Realtime (speech-to-speech). This is the ONLY voice path on this
# branch - the previous Groq Whisper -> LLaMA/FSM -> edge-tts cascade has
# been fully removed here (it still exists on `main`, untouched, in case
# S2S doesn't clear accuracy parity and this branch gets abandoned).
# --------------------------------------------------------------------------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Pinned to a specific snapshot on purpose, not an auto-updating alias -
# gpt-realtime-2.1-mini shipped July 7 2026 (one day before this was
# written) with zero community track record yet. Confirm the exact
# snapshot string in the OpenAI dashboard model list before changing this,
# and only move off "gpt-realtime-mini" once 2.1-mini has a few weeks of
# real production usage behind it. Cost is identical between the two per
# OpenAI's published pricing, so there's no cost reason to rush the switch.
REALTIME_MODEL = os.environ.get("REALTIME_MODEL", "gpt-realtime-2.1-mini")

REALTIME_VOICE = os.environ.get("REALTIME_VOICE", "alloy")

# This is a phone/speakerphone bot, not a headset bot - callers will be on
# cars, shops, kitchens. far_field + a raised threshold is the deliberate
# default; near_field + 0.5 is tuned for close headset mics and will
# false-trigger on background noise for this use case.
REALTIME_VAD_MODE = os.environ.get("REALTIME_VAD_MODE", "far_field")
REALTIME_VAD_TYPE = os.environ.get("REALTIME_VAD_TYPE", "semantic_vad")  # semantic_vad | server_vad
REALTIME_VAD_EAGERNESS = os.environ.get("REALTIME_VAD_EAGERNESS", "medium")  # medium = middle ground between "low" (waits longest, fewest false turn-ends) and "high" (fastest, most prone to cutting the caller off mid-sentence). Moved from low after a client speed complaint - re-test carefully for premature cutoffs before pushing to "high".
REALTIME_VAD_THRESHOLD = float(os.environ.get("REALTIME_VAD_THRESHOLD", "0.7"))
REALTIME_VAD_SILENCE_MS = int(os.environ.get("REALTIME_VAD_SILENCE_MS", "800"))  # was 700 - a real test call had semantic_vad close the turn right after "I" (before "...arrange a call" finished), causing the model to guess instead of hearing the full sentence. +200ms costs a little latency but gives mid-sentence pauses more room before the turn is called done.

REALTIME_WS_URL = os.environ.get("REALTIME_WS_URL", "wss://api.openai.com/v1/realtime")

# Cost-safety: OpenAI bills the Realtime connection per-minute while it's
# open, regardless of whether anyone is talking. Nothing previously ended
# the call server-side - reaching the "closing" stage only sent a UI
# update, so a caller who didn't hang up (or a browser tab left open)
# would keep the connection billing indefinitely.
REALTIME_CALL_END_GRACE_SECONDS = float(os.environ.get("REALTIME_CALL_END_GRACE_SECONDS", "2.0"))  # let the closing line finish playing before hanging up
REALTIME_IDLE_TIMEOUT_SECONDS = float(os.environ.get("REALTIME_IDLE_TIMEOUT_SECONDS", "120"))  # no audio/events from either side for this long -> hang up
# Separate from the idle timeout above: this catches OpenAI's own semantic_vad
# never deciding the caller finished talking (turn just never closes), which
# still counts as "activity" (audio keeps streaming in) so the idle timeout
# above never trips. If we've been waiting this long since asking a question
# with no speech_stopped/response, force a manual buffer commit instead of
# waiting indefinitely - see RealtimeSession._watchdog.
REALTIME_STUCK_TURN_SECONDS = float(os.environ.get("REALTIME_STUCK_TURN_SECONDS", "22"))  # was 12 - real test calls show normal think-time on multi-choice questions running 12-13s, so 12 was firing on ordinary pauses, not genuine stuck VAD
REALTIME_MAX_CALL_SECONDS = float(os.environ.get("REALTIME_MAX_CALL_SECONDS", "300"))  # absolute hard cap per call regardless of activity

# --------------------------------------------------------------------------
# Everything below is voice-provider-agnostic: lead delivery, DB, CORS.
# None of it was Groq-specific, so none of it changed.
# --------------------------------------------------------------------------
WP_SUBMIT_ENDPOINT = os.environ.get("WP_SUBMIT_ENDPOINT", "")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
NOTIFY_EMAIL_TO = os.environ.get("NOTIFY_EMAIL_TO", "")
NOTIFY_EMAIL_FROM = os.environ.get("NOTIFY_EMAIL_FROM", "") or SMTP_USER

CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")
LEADS_FALLBACK_FILE = os.environ.get("LEADS_FALLBACK_FILE", "leads_fallback.jsonl")
APP_DB_PATH = os.environ.get("APP_DB_PATH", "quote_assistant.sqlite3")
ABANDONED_LEAD_IDLE_SECONDS = int(os.environ.get("ABANDONED_LEAD_IDLE_SECONDS", "900"))
ABANDONED_LEAD_SWEEP_SECONDS = int(os.environ.get("ABANDONED_LEAD_SWEEP_SECONDS", "120"))

# Optional Google Calendar booking for scheduled callbacks - see
# services/calendar_service.py. Blank GOOGLE_SERVICE_ACCOUNT_FILE = skipped
# gracefully, booking/lead-email still happen either way.
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
# Railway/most PaaS hosts can't hold an arbitrary file from a gitignored
# path - paste the service account JSON's full contents as one env var
# instead. If both are set, the JSON var wins (see calendar_service.py).
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
CALENDAR_TIMEZONE = os.environ.get("CALENDAR_TIMEZONE", "America/Chicago")
# How far ahead a caller can book a callback/visit. Paired with the
# schedule_appointment instructions in realtime_session.py (which now
# accept any date phrasing, not just 'today'/'tomorrow') - this is the
# actual server-side backstop, since a model-computed date should never
# be trusted blindly. See services/realtime_tools.py's handle_schedule_appointment.
SCHEDULE_MAX_DAYS_AHEAD = int(os.environ.get("SCHEDULE_MAX_DAYS_AHEAD", "30"))
# Fixed slot grid for the schedule_appointment calendar widget (frontend
# generates slots client-side from these three, so they only need to
# change in one place). Times are in CALENDAR_TIMEZONE, 24-hour HH:MM.
SCHEDULE_BUSINESS_HOURS_START = os.environ.get("SCHEDULE_BUSINESS_HOURS_START", "09:00")
SCHEDULE_BUSINESS_HOURS_END = os.environ.get("SCHEDULE_BUSINESS_HOURS_END", "18:00")
SCHEDULE_SLOT_MINUTES = int(os.environ.get("SCHEDULE_SLOT_MINUTES", "30"))
# Domain-wide delegation subject - a real Workspace mailbox the service
# account impersonates so it's allowed to invite attendees and actually
# send the customer a calendar email. Blank = events are still created
# on the team calendar, but customers are NOT emailed an invite (Google
# rejects attendee invites from a bare service account). See
# services/calendar_service.py module docstring for setup steps.
GOOGLE_DELEGATED_USER = os.environ.get("GOOGLE_DELEGATED_USER", "")

# API-as-a-service gating (see services/session_store.py's api_keys table
# and main.py's require_api_key dependency). Set this to something only
# you know - it's the master secret for POSTing new client API keys via
# /api/admin/keys, not a per-client key.
ADMIN_API_SECRET = os.environ.get("ADMIN_API_SECRET", "")
REQUIRE_API_KEY = os.environ.get("REQUIRE_API_KEY", "false").lower() in ("1", "true", "yes", "on")

# Optional real outbound call for "call me right now" - see
# services/call_service.py. Blank = urgent email only (still works fully
# without this - Twilio needs its own paid account/number to set up).
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")
ORG_PHONE_NUMBER = os.environ.get("ORG_PHONE_NUMBER", "")

DEFAULT_COUNTRY_CODE = os.environ.get("DEFAULT_COUNTRY_CODE", "+91")
REALTIME_CALL_END_GRACE_SECONDS = float(os.environ.get("REALTIME_CALL_END_GRACE_SECONDS", "4.0"))