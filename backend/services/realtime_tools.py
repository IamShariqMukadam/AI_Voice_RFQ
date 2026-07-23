"""
Tool schemas + handlers for the S2S (OpenAI Realtime) path.

These wrap the EXACT same functions the cascaded pipeline already uses -
dialogue.state_machine.DialogueManager, services.plan_matcher,
services.notify, services.session_store, services.calendar_service.
Nothing about pricing, validation, or FSM stage order/business rules is
reimplemented here; this file only exposes them as callable tools plus
guard rails a raw function call doesn't give you for free.

Anti-hallucination guarantee: confirm_slot's reply already contains the
exact FSM-templated next line (same string the cascaded path would
speak), including any plan prices - so reading that back verbatim is
always safe. get_plan_pricing exists on top of that for cases where the
caller asks to hear prices again later without re-confirming location.
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from dialogue import slots as S
from dialogue.state_machine import DialogueManager
from services import calendar_service, extraction, notify, plan_matcher, session_store

# BUG FIX: calendar_service.create_call_event() (Google Calendar API) and
# dm._advance()'s lead-email send (blocking SMTP) used to run one after
# the other inside a single asyncio.to_thread call - both slow network
# calls, stacked sequentially, produced ~30+ seconds of dead air for the
# caller waiting on the line during schedule_appointment. Running them
# on separate threads here overlaps them, so the wait is roughly
# max(calendar, email) instead of their sum.
_SCHEDULE_POOL = ThreadPoolExecutor(max_workers=4)

logger = logging.getLogger("realtime_tools")

# Fields where the caller might not realize they CAN change their answer
# (they didn't build this thing - unlike a developer, they don't assume
# a "back" option exists). A short spoken reminder gets appended to
# say_next for exactly these three, once, right after they're answered.

TOOL_SCHEMAS = [
    {
        "type": "function",
        "name": "get_plan_pricing",
        "description": (
            "Look up real available plans/prices for a category+tonnage+"
            "location combo. You may ONLY speak a price or plan name that "
            "appears in a get_plan_pricing or confirm_slot result in THIS "
            "conversation - never state a price from memory or estimate one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "tonnage": {"type": "string"},
                "location": {"type": "string"},
            },
            "required": ["category", "tonnage", "location"],
        },
    },
    {
        "type": "function",
        "name": "go_back_or_edit",
        "description": (
            "Handles anything that isn't a forward answer: the caller "
            "wants to go back one question, change something they already "
            "answered, hear the current question again, or start the "
            "whole call over. Always use this instead of trying to force "
            "it through confirm_slot - confirm_slot will reject an "
            "out-of-order field."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["go_back", "edit_field", "repeat", "start_over"],
                    "description": (
                        "go_back: step to the previous question. "
                        "edit_field: jump to re-ask a specific already-answered field (requires 'field'). "
                        "repeat: re-read the current question, nothing changes. "
                        "start_over: wipe everything and restart from full_name."
                    ),
                },
                "field": {
                    "type": "string",
                    "description": "Required only for action='edit_field'.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "type": "function",
        "name": "save_lead_to_db",
        "description": (
            "Force-saves current progress immediately. Rarely needed: "
            "the backend already silently saves progress after every "
            "successful confirm_slot call, so calling this too is "
            "usually redundant work that costs an extra response with "
            "nothing new to say. Only call this for an unusual event "
            "with no confirm_slot involved (e.g. the caller says "
            "they're about to lose signal) - not as a routine follow-up."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "is_complete": {
                    "type": "boolean",
                    "description": "True only once the closing stage has been reached.",
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "schedule_appointment",
        "description": (
            "Books a specific date+time for either a callback or a "
            "technician visit (whichever the caller is currently "
            "scheduling - you don't need to specify which, the backend "
            "already knows). Only valid when the current stage is "
            "'schedule_appointment'. The caller can name a date however "
            "they like (today/tomorrow, a weekday name, an explicit "
            "date) - resolve it to an exact calendar date yourself (see "
            "the schedule_appointment instructions), confirm the exact "
            "slot with the caller, THEN call this - if the slot is "
            "already booked, or the date is out of the bookable window, "
            "you'll get an error back and should offer a different time/date."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "call_date": {
                    "type": "string",
                    "description": "YYYY-MM-DD - the exact date you resolved from whatever the caller said (weekday name, explicit date, 'today'/'tomorrow'), using the current date given to you in your instructions. Never guess a year/month - compute it.",
                },
                "call_time": {
                    "type": "string",
                    "description": "24-hour HH:MM, e.g. '14:30'.",
                },
            },
            "required": ["call_date", "call_time"],
        },
    },
]


def _valid_options_for(session, field):
    """Returns the set of values actually offered for `field` right now.
    Static fields come from dialogue/slots.py; plan_choice/plan_action
    are dynamic (depend on available_plans/chosen plans) so they're read
    from DialogueManager._stage_meta instead - this closes a gap where
    those two previously had NO server-side validation at all, since
    they aren't in dialogue.slots.STAGES."""
    meta = S.STAGES.get(field)
    if meta and meta.get("kind") == "choice":
        return {o["value"] for o in meta["options"]}
    if field in ("plan_choice", "plan_action", "review_summary", "call_timing"):
        dyn = DialogueManager(session)._stage_meta(field)
        if dyn:
            return {o["value"] for o in dyn["options"]}
    return None


def option_label(session, field, value):
    """Human-readable label for a tapped option's raw value, across both
    static (dialogue.slots.STAGES) and dynamic (plan_choice/plan_action/
    review_summary) fields - e.g. 'closet_vertical' -> 'Closet (Vertical)'.
    Used to leave a readable trace of on-screen taps in the live OpenAI
    conversation (see realtime_session.py's select_option handler) instead
    of the raw slug, since raw slugs read as gibberish in a transcript."""
    meta = S.STAGES.get(field) or DialogueManager(session)._stage_meta(field)
    lookup = {o["value"]: o["label"] for o in (meta or {}).get("options", [])}
    if field == "plan_choice":
        ids = [x.strip() for x in (value or "").split(",") if x.strip()]
        return " and ".join(lookup.get(i, i) for i in ids) or str(value)
    return lookup.get(value, str(value))


def _confirm_slot_schema(session):
    """Builds confirm_slot's schema fresh for the session's CURRENT stage
    only. This is the actual fix for the category/tonnage confirmation-
    loop bug: the old static schema let 'value' be any free string, so
    for choice fields (category, tonnage, location...) the model had to
    invent the underscore-slug itself from memory of the spoken prompt
    (e.g. guessing 'cooling_electric_heat' vs 'cooling_heat_pump') with
    nothing grounding it to the real option ids. A wrong-but-valid guess
    passed validation, the FSM advanced on bad data, and the model then
    tried to conversationally correct it while still guessing the same
    wrong id - the endless "let's correct that / are you sure" loop.
    Locking 'field' to the one legal value and 'value' to an explicit
    enum (with human labels spelled out) makes a wrong id essentially
    impossible instead of just being told not to guess."""
    stage = session.stage
    options = _valid_options_for(session, stage)

    if stage in ("phone", "zip"):
        # BUG FIX: realtime speech-to-speech models can blur/merge
        # repeated digits when converting heard audio into a compact
        # number (e.g. hearing "seven three seven eight EIGHT five oh
        # five five eight" and passing "73785055" - silently dropping one
        # of the repeated 8s). Requiring space-separated single digits
        # forces counting each one individually instead of mentally
        # compressing into "a number", which is OpenAI's own documented
        # mitigation for this exact failure mode.
        value_schema = {
            "type": "string",
            "description": (
                "The digits the caller said, as single digits separated "
                "by spaces (e.g. '7 3 7 8 8 5 0 5 5 8'), in the order "
                "said. Count every digit individually - a REPEATED digit "
                "('eight eight', 'double eight', 'five five') is TWO "
                "digits, write both, never collapse a repeat into one. "
                "If the audio was unclear, silent, or you're not confident "
                "you heard actual digits, do NOT guess and do NOT reuse "
                "digits from an earlier field (e.g. the phone number) - "
                "ask the caller to repeat instead of calling confirm_slot."
            ),
        }
    elif not options:
        value_schema = {
            "type": "string",
            "description": (
                "The caller's answer in plain text - their actual words, "
                "spelled normally (e.g. 'Alex Rivera', a plain email "
                "address). Do NOT invent a phonetic breakdown of how a "
                "name sounds (never things like 'Sha-riq' or 'Saa-rik') - "
                "if you're unsure of spelling, use the most ordinary "
                "spelling of what you heard. The one exception: if the "
                "caller explicitly spells something out letter-by-letter "
                "('s-h-a-r-i-q') or gives a correction ('with a q', 'no, k "
                "not c'), pass exactly those letters/words, unmodified, in "
                "the order said - a deterministic step downstream applies "
                "that correction, but only from your literal transcription "
                "of it, not your own guess."
            ),
        }
    else:
        meta = S.STAGES.get(stage) or DialogueManager(session)._stage_meta(stage)
        opt_list = (meta or {}).get("options", [])
        pairs = "; ".join(f"{o['value']} = \"{o['label']}\"" for o in opt_list)
        if stage == "plan_choice":
            # Multi-select field - value is a comma-separated list of ids,
            # so a strict JSON enum doesn't fit. Spell out valid ids/labels
            # in the description instead, which is what actually matters.
            value_schema = {
                "type": "string",
                "description": (
                    f"One or more plan ids, comma-separated if more than "
                    f"one. Valid ids and what they mean: {pairs}. Use the "
                    f"id (left side), never the label."
                ),
            }
        else:
            value_schema = {
                "type": "string",
                "enum": sorted(options),
                "description": (
                    f"Must be EXACTLY one of the enum values. What each one "
                    f"means: {pairs}. Match the caller's words to the "
                    f"correct id - do not guess if unsure, ask them to repeat."
                ),
            }

    properties = {
        "field": {
            "type": "string",
            "enum": [stage],
            "description": f"Always exactly '{stage}' - the caller's current question.",
        },
        "value": value_schema,
    }
    description = (
        "Record the caller's answer for the CURRENT stage's field as "
        "soon as you have a clear value. Do NOT read it back for "
        "confirmation first - that happens once in bulk later, at "
        "review_summary, not per field. The tool result's say_next "
        "field is the exact next line to speak - use it as-is. Right "
        f"now the only valid field is '{stage}'."
    )

    if stage == "plan_action":
        # BUG FIX (compound answer at the plan_action/call_timing fork):
        # a caller who says "arrange a call, right now" has answered
        # plan_action AND call_timing in the same breath. Without this,
        # the model either drops the second half (asks call_timing again)
        # or tries to save it via a second confirm_slot call that gets
        # rejected by the field-lock guard (field != stage, since stage
        # is still 'plan_action' on that turn) - that rejection is the
        # exact stall this fixes. Making the co-answer a legal optional
        # argument here means the model never has to guess ahead into an
        # out-of-order call.
        timing_options = _valid_options_for(session, "call_timing") or set()
        properties["call_timing"] = {
            "type": "string",
            "enum": sorted(timing_options),
            "description": (
                "ONLY include this if the caller ALSO stated their call "
                "timing preference in the same breath as choosing "
                "'arrange_call' (e.g. 'call me back, right now' or "
                "'have someone call today'). Omit entirely if they only "
                "answered plan_action - do not guess this."
            ),
        }

    return {
        "type": "function",
        "name": "confirm_slot",
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": ["field", "value"],
        },
    }


def _with_review_reminder(field, speech_text):
    # BUG FIX (user request): this used to append "by the way you can
    # change your X anytime" after tonnage/location/plan_choice. Now
    # that every field is editable via the always-visible per-field
    # "Change" button AND corrected in bulk at review_summary, this
    # extra line just wastes a sentence (and tokens) on every turn.
    return speech_text

_GREETING_FILLERS = {
    "hi", "hello", "hey", "hiya", "yo", "sup",
    "hi there", "hello there", "good morning", "good afternoon",
    "good evening", "greetings", "whats up",
    "alex rivera",  # the literal placeholder from the tool schema example
}

def _is_greeting_filler(value: str) -> bool:
    normalized = re.sub(r"[^a-z\s]", "", (value or "").lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized in _GREETING_FILLERS

def handle_confirm_slot(session, field, value, call_timing=None):
    """Mirrors DialogueManager.handle_manual - the same validation path
    the cascaded pipeline's typed/manual-input flow already uses, so a
    value that wouldn't be accepted there can't sneak through here.

    Guards on top of a raw function call:
    - field must match the caller's actual current stage (a wrong tool
      call could otherwise silently jump the FSM forward). Editing an
      earlier field goes through go_back_or_edit first, which sets
      session.stage to that field BEFORE confirm_slot runs - so this
      never blocks a legitimate edit.
    - phone gets digit-accumulation handling for numbers split across
      two turns instead of a flat accept/reject.
    - plan_choice accepts a comma-separated list of ids (multi-plan
      selection) - each id is validated against what's actually on offer.
    - tonnage/location/plan_choice get a spoken "you can change this"
      reminder appended, since a first-time caller has no way to know
      an edit/back option even exists otherwise.
    """
    if not field:
        return {"ok": False, "error": "missing field"}
    if session.stage == "closing":
        # BUG FIX: the model was calling confirm_slot again after the
        # call already reached the terminal 'closing' stage (it saw
        # 'stage': 'closing' in the previous tool result and treated
        # that like just another field to confirm). 'closing' isn't a
        # real field in S.ORDER, so this used to fall through to
        # _advance()'s S.ORDER.index(stage) and crash with a
        # ValueError. Nothing more to confirm once we're here.
        return {"ok": False, "error": "the call is already complete - nothing left to confirm, do not call any more tools"}
    if field != session.stage:
        return {
            "ok": False,
            "error": (
                f"'{field}' is not the current question - the caller is on '{session.stage}'. "
                "If they want to change an earlier answer, call go_back_or_edit with "
                "action='edit_field' first."
            ),
        }
    
    if field == "full_name" and _is_greeting_filler(value):
        return {
            "ok": False,
            "error": "that's a greeting, not a name - ask the caller for their full name again",
        }

    if field == "phone":
        digits = extraction.extract_digits(value or "")
        if not digits:
            return {"ok": False, "error": "no digits found in that value"}
        dm = DialogueManager(session)
        previous_stage = session.stage
        _, speech_text = dm._handle_phone_digits(digits)
        session_store.save_progress(session, is_complete=session.stage == "closing")
        return {
            "ok": True,
            "stage": session.stage,
            "say_next": speech_text,
            "complete": session.stage != previous_stage,
        }

    if field == "plan_choice":
        ids = [x.strip() for x in (value or "").split(",") if x.strip()]
        valid_ids = _valid_options_for(session, "plan_choice") or set()
        if not ids:
            return {"ok": False, "error": "no plan selected"}
        invalid = [i for i in ids if i not in valid_ids]
        if invalid:
            return {"ok": False, "error": f"{invalid} not a valid plan id. Valid options: {sorted(valid_ids)}"}
        dm = DialogueManager(session)
        _, speech_text = dm.handle_manual("plan_choice", ids)
        session_store.save_progress(session, is_complete=session.stage == "closing")
        return {
            "ok": True, "stage": session.stage,
            "say_next": _with_review_reminder("plan_choice", speech_text),
        }

    options = _valid_options_for(session, field)
    if options is not None and value not in options:
        return {
            "ok": False,
            "error": f"'{value}' is not a valid option for {field}. Valid options: {sorted(options)}",
        }
    dm = DialogueManager(session)
    _, speech_text = dm.handle_manual(field, value)

    if (
        field == "plan_action"
        and value == "arrange_call"
        and call_timing
        and session.stage == "call_timing"
    ):
        timing_options = _valid_options_for(session, "call_timing") or set()
        if call_timing in timing_options:
            _, speech_text = dm.handle_manual("call_timing", call_timing)
        # an invalid/unrecognized call_timing value is silently ignored
        # here (not an error) - the caller still legitimately answered
        # plan_action, so that half must not be thrown away; call_timing
        # just gets asked normally on the next turn instead.

    session_store.save_progress(session, is_complete=session.stage == "closing")
    return {
        "ok": True,
        "stage": session.stage,
        "say_next": _with_review_reminder(field, speech_text),
    }


def handle_get_plan_pricing(session, category, tonnage, location):
    plans = plan_matcher.get_available_plans(category, tonnage, location)
    session.available_plans = plans
    return {
        "plans": [
            {"id": p["id"], "name": p["name"], "price_display": p["price_display"], "features": p.get("features", [])}
            for p in plans
        ]
    }


def handle_save_lead_to_db(session, is_complete=False):
    session_store.save_progress(session, is_complete=bool(is_complete) or session.stage == "closing")
    if session.stage == "closing":
        DialogueManager(session)._submit_lead_once()
    return {"ok": True}


def handle_go_back_or_edit(session, action, field=None):
    dm = DialogueManager(session)

    if action == "repeat":
        _, speech_text = dm._entry_text(session.stage)
        return {"ok": True, "stage": session.stage, "say_next": speech_text}

    if action == "start_over":
        _, speech_text = dm.restart()
        session_store.save_progress(session, is_complete=False)
        return {"ok": True, "stage": session.stage, "say_next": speech_text}

    if action == "go_back":
        _, speech_text = dm.go_back()
        session_store.save_progress(session, is_complete=session.stage == "closing")
        return {"ok": True, "stage": session.stage, "say_next": speech_text}

    if action == "edit_field":
        if not field:
            return {"ok": False, "error": "edit_field requires 'field'"}
        if field not in S.STAGES and field not in ("plan_choice", "schedule_appointment"):
            return {"ok": False, "error": f"'{field}' can't be edited"}
        if field not in session.slots:
            return {"ok": False, "error": "that field hasn't been answered yet - finish the current question first"}
        _, speech_text = dm.jump_to(field)
        session_store.save_progress(session, is_complete=False)
        return {"ok": True, "stage": session.stage, "say_next": speech_text}

    return {"ok": False, "error": f"unknown action '{action}'"}


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def handle_schedule_appointment(session, call_date, call_time):
    if session.stage != "schedule_appointment":
        return {"ok": False, "error": f"not the current stage - caller is on '{session.stage}'"}
    if not call_date or not _DATE_RE.match(call_date):
        return {"ok": False, "error": "call_date must be YYYY-MM-DD"}
    if not call_time or not _TIME_RE.match(call_time):
        return {"ok": False, "error": "call_time must be 24-hour HH:MM"}

    # BUG FIX: the instructions now let the caller name a date any way
    # they like (weekday name, explicit date, 'today'/'tomorrow') and
    # have the MODEL resolve it to YYYY-MM-DD - which means a model date-
    # math mistake (wrong year, resolving 'Sunday' to next month, etc.)
    # would previously sail straight through, since the only server-side
    # check was the regex format above. This is the actual backstop:
    # reject anything before today or past the bookable window, in the
    # org's own calendar timezone (not the server's/caller's, which can
    # differ), with an error worded so the model can relay it and ask
    # again instead of just failing silently.
    today = datetime.now(ZoneInfo(config.CALENDAR_TIMEZONE)).date()
    try:
        requested = datetime.strptime(call_date, "%Y-%m-%d").date()
    except ValueError:
        return {"ok": False, "error": "call_date must be YYYY-MM-DD"}
    days_out = (requested - today).days
    if days_out < 0:
        return {"ok": False, "error": f"{call_date} is in the past - ask the caller for today or a future date"}
    if days_out > config.SCHEDULE_MAX_DAYS_AHEAD:
        return {
            "ok": False,
            "error": (
                f"{call_date} is too far out - bookings are only accepted within "
                f"{config.SCHEDULE_MAX_DAYS_AHEAD} days from today ({today.isoformat()}). "
                "Ask the caller for a closer date."
            ),
        }

    if session_store.is_slot_booked(call_date, call_time):
        return {"ok": False, "error": f"{call_date} {call_time} is already booked - offer the caller a different time"}

    appt_type = session.slots.get("appointment_type", "call")
    booked = session_store.book_call_slot(call_date, call_time, session.session_id, appointment_type=appt_type)
    if not booked:
        # Lost a race to another caller between the check above and the
        # insert - vanishingly rare, but handled rather than crashing.
        return {"ok": False, "error": f"{call_date} {call_time} was just booked by someone else - offer a different time"}

    session.slots["appointment_date"] = call_date
    session.slots["appointment_time"] = call_time

    # Create the calendar event BEFORE generating the closing line, so
    # _closing_text() can check whether the customer was actually
    # invited and only promise a calendar email if one was really sent
    # (a bare service account without domain-wide delegation can create
    # the event but is not allowed to invite attendees - see
    # calendar_service.py).
    dm = DialogueManager(session)
    cal_future = _SCHEDULE_POOL.submit(calendar_service.create_call_event, session, call_date, call_time)
    adv_future = _SCHEDULE_POOL.submit(dm._advance, "schedule_appointment", None)  # submits the lead exactly once, advances to closing

    calendar_result = cal_future.result()
    session.slots["_calendar_link"] = calendar_result.get("link") if calendar_result else None
    session.slots["_calendar_attendee_invited"] = bool(calendar_result and calendar_result.get("attendee_invited"))
    session.stage = adv_future.result()
    _, closing_speech = dm._entry_text(session.stage)

    session_store.save_progress(session, is_complete=True)
    return {"ok": True, "stage": session.stage, "say_next": closing_speech}


_DISPATCH = {
    "confirm_slot": lambda session, args: handle_confirm_slot(session, args.get("field"), args.get("value"), args.get("call_timing")),
    "get_plan_pricing": lambda session, args: handle_get_plan_pricing(
        session, args.get("category"), args.get("tonnage"), args.get("location")
    ),
    "save_lead_to_db": lambda session, args: handle_save_lead_to_db(session, args.get("is_complete", False)),
    "go_back_or_edit": lambda session, args: handle_go_back_or_edit(session, args.get("action"), args.get("field")),
    "schedule_appointment": lambda session, args: handle_schedule_appointment(session, args.get("call_date"), args.get("call_time")),
}


def call_tool(session, name: str, arguments: dict):
    fn = _DISPATCH.get(name)
    if not fn:
        return {"ok": False, "error": f"unknown tool {name}"}
    try:
        return fn(session, arguments or {})
    except Exception:
        logger.exception("[%s] tool %s failed", getattr(session, "session_id", "?"), name)
        return {"ok": False, "error": "internal error handling this tool call"}


def tools_for_stage(session):
    """Stage-gated tool exposure. Every stage (except closing) gets a
    confirm_slot schema built fresh for THAT stage (see
    _confirm_slot_schema) plus save_lead_to_db/go_back_or_edit;
    location/plan_choice/plan_action additionally get pricing lookup;
    schedule_appointment's own tool is only exposed once the caller has
    actually reached that stage."""
    stage = session.stage
    active = {"save_lead_to_db", "go_back_or_edit"}
    if stage in ("location", "plan_choice", "plan_action"):
        active.add("get_plan_pricing")
    if stage == "schedule_appointment":
        active.add("schedule_appointment")
    tools = [t for t in TOOL_SCHEMAS if t["name"] in active]
    # BUG FIX: confirm_slot used to be offered at every non-closing stage,
    # including schedule_appointment - which gave the model an easy wrong
    # shortcut: call confirm_slot(field='schedule_appointment', value='today,
    # 18:00') instead of the dedicated schedule_appointment tool. confirm_slot
    # just stores that raw string and closes the call - it never checks slot
    # conflicts, never sets appointment_date/appointment_time, and never
    # creates the calendar event, so the call would "complete" with no actual
    # booking and no calendar invite. Excluding confirm_slot here forces the
    # only tool that can legally end this stage to be the real one.
    if stage != "closing" and stage != "schedule_appointment":
        tools.append(_confirm_slot_schema(session))
    return tools