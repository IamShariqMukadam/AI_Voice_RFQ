import logging
import re
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
from dialogue import slots as S
from services import call_service, extraction, notify, plan_matcher, session_store

logger = logging.getLogger("dialogue")


PERSONAL_INFO_FIELDS = {"full_name", "phone", "email", "street", "city", "zip"}

EDIT_VERBS = ("edit", "change", "update", "fix", "correct", "redo")
FIELD_SYNONYMS = [
    ("full_name", ("full name", "my name", "name")),
    ("phone", ("phone number", "my phone", "phone", "number")),
    ("email", ("email address", "e mail", "email", "mail")),
    ("zip", ("zip code", "postal code", "pin code", "zip")),
    ("street", ("street address", "street")),
    ("city", ("city",)),
    ("category", ("system type", "heating or cooling", "category", "system")),
    ("tonnage", ("tonnage", "system size", "size", "ton")),
    ("location", ("air handler", "location", "handler")),
    ("plan_choice", ("plan",)),
]


def _find_edit_field(t: str):
    """Looks for an edit-intent verb plus a field name anywhere in the
    utterance ('can you change my email', 'fix the zip code'). Both must
    be present so a normal answer never accidentally triggers this."""
    if not any(re.search(rf"\b{v}\b", t) for v in EDIT_VERBS):
        return None
    for field, synonyms in FIELD_SYNONYMS:
        if any(re.search(rf"\b{s}\b", t) for s in synonyms):
            return field
    return None


class DialogueManager:
    """Owns stage transitions for one session. Every spoken line comes
    from a plain string template here - never from the LLM - so the
    assistant can never misquote a price or a plan name.

    Every public method here returns a (display_text, speech_text) pair.
    They're usually identical; they differ only for choice questions
    where the options are already shown as clickable cards, so the
    on-screen text stays short while the spoken version still reads out
    the full list for anyone going hands-free."""

    def __init__(self, session):
        self.session = session

    # ---- entry points called from main.py -------------------------------

    def greeting(self):
        self.session.stage = "full_name"
        prompt = S.STAGES["full_name"]["prompt"]
        speech = f"{prompt} Press the black speech button to talk."
        return prompt, speech

    def handle_turn(self, transcript: str):
        transcript = (transcript or "").strip()
        stage = self.session.stage

        meta_reply = self._check_meta_intent(transcript, stage)
        if meta_reply:
            return meta_reply

        if stage == "closing":
            text = "That request is already complete. Refresh the page to start a new one."
            return text, text

        if stage == "phone":
            phone_reply = self._handle_phone_turn(transcript)
            if phone_reply:
                return phone_reply

        meta = self._stage_meta(stage)
        value = extraction.extract(stage, transcript, meta)
        if value is None:
            self.session.voice_fail_count += 1
            hint = meta["prompt"].format(**self.session.slots) if meta else ""
            nudge = " If it's easier, you can just type your answer in the box below." if self.session.voice_fail_count >= 2 else ""
            text = f"Sorry, I didn't quite catch that. {hint}{nudge}".strip()
            return text, text
        return self._apply_value(stage, value)

    def handle_manual(self, field: str, value: str):
        meta = self._stage_meta(field)
        kind = (meta or {}).get("kind")
        if kind in ("phone", "email", "zip"):
            cleaned = extraction.extract(field, value, meta)
            if cleaned is None:
                text = {
                    "phone": "That doesn't look like a valid phone number - please enter 10 digits.",
                    "email": "That doesn't look like a valid email - please enter one like name@example.com.",
                    "zip": "That doesn't look like a valid zip code.",
                }[kind]
                return text, text
            value = cleaned
        return self._apply_value(field, value)

    def _handle_phone_turn(self, transcript: str):
        """Phone numbers are easy to split accidentally: the recorder may
        stop after 8-9 digits, then the caller says the final digit on the
        next turn. Remember short digit fragments instead of treating each
        fragment as a brand-new invalid phone number."""
        digits = extraction.extract_digits(transcript)
        if not digits:
            return None
        return self._handle_phone_digits(digits)

    def _handle_phone_digits(self, digits: str):
        """Same digit-accumulation behavior as _handle_phone_turn, split
        out so callers that already have extracted digits (rather than a
        raw transcript to run extraction on) can reuse it - see
        services/realtime_tools.py, which gets a value the S2S model has
        already pulled out of audio, not a transcript to re-parse."""
        partial = self.session.partial_inputs.get("phone", "")
        # BUG FIX: this used to always glue the new digits onto whatever
        # partial fragment was already stored (`partial + digits`), with
        # no way to tell "the caller said the rest" apart from "the
        # caller, confused by the 'remaining N digits' prompt, just said
        # the WHOLE number again". In that second case the fresh digits
        # were already a complete, correct 10-digit number on their own,
        # but gluing an old 9-digit fragment onto a new 10-digit one gave
        # 19 digits - which then failed as "not a 10-digit number" even
        # though what the caller just said was perfectly valid. Try the
        # newly-said digits alone first; only fall back to combining with
        # the stored fragment if they don't already form a full number.
        cleaned = extraction.clean_phone(digits)
        if cleaned:
            self.session.partial_inputs.pop("phone", None)
            return self._apply_value("phone", cleaned)

        # BUG FIX: something 7+ digits long is almost certainly a fresh
        # attempt at the WHOLE number, not "the last couple digits" a
        # caller was just asked for (that's normally 1-4 digits). Gluing
        # a long-but-still-imperfect new attempt onto an old, differently-
        # wrong partial produced garbage (e.g. an 8-digit mishearing +
        # a 9-digit mishearing glued into 17 digits, which then failed
        # outright even though the caller had just said the number
        # correctly). Replace the stale partial instead of combining with
        # it whenever the new fragment is long enough to be a full retry.
        if len(digits) >= 7:
            partial = ""

        combined = partial + digits
        cleaned = extraction.clean_phone(combined)
        if cleaned:
            self.session.partial_inputs.pop("phone", None)
            return self._apply_value("phone", cleaned)

        if len(combined) < 10:
            self.session.partial_inputs["phone"] = combined
            remaining = 10 - len(combined)
            digit_word = "digit" if remaining == 1 else "digits"
            # BUG FIX: previously said only "I heard N digits so far" with
            # no readback of WHAT was heard. If the model mis-transcribed
            # a digit from audio (which it does sometimes - a real audio
            # accuracy limit, not something a prompt can fully fix), that
            # wrong digit sat silently in `partial` and kept getting glued
            # onto every later attempt, compounding into a corrupted
            # number over several turns with the caller never able to
            # tell anything was wrong. Reading the digits back lets them
            # catch a bad one immediately and just say the whole number
            # again instead.
            spoken_digits = " ".join(combined)
            text = (
                f"I heard {spoken_digits} - that's {len(combined)} digits "
                f"so far. If that's right, say the remaining {remaining} "
                f"{digit_word}. If I misheard something, just say the "
                f"full 10-digit number again."
            )
            return text, text

        self.session.partial_inputs.pop("phone", None)
        self.session.voice_fail_count += 1
        text = "That didn't sound like a 10-digit phone number. Please say the full phone number again."
        return text, text

    def go_back(self):
        """Step to the previous stage without losing any already-collected
        slots, so a misclick never forces a full page reload."""
        stage = self.session.stage
        if stage == "closing" or stage not in S.ORDER:
            text = "We can't go back from here - refresh the page to start a new request."
            return text, text
        idx = S.ORDER.index(stage)
        if idx == 0:
            text = "We're already at the very first question."
            return text, text
        prev = S.ORDER[idx - 1]
        self.session.stage = prev
        return self._entry_text(prev)

    def jump_to(self, field: str):
        """Used by the 'edit' link on an already-answered field (or a
        spoken 'change my phone number' - see _check_meta_intent): re-asks
        that exact question, and remembers where we were so an edit to a
        personal-info field can resume there afterward instead of
        restarting forward progress. Fields that other questions branch
        on (category/tonnage/location) intentionally do NOT resume - an
        edited category legitimately needs to re-walk tonnage/location/
        plan since those depend on it."""
        if field not in S.STAGES and field not in ("plan_choice", "schedule_appointment"):
            text = "That field can't be edited."
            return text, text
        if self.session.stage != field:
            self.session.resume_stage = self.session.stage
        self.session.stage = field
        return self._entry_text(field)

    def ui_for_stage(self):
        stage = self.session.stage
        if stage == "closing":
            return {"type": "none"}
        if stage == "schedule_appointment":
            return self._schedule_ui()
        meta = self._stage_meta(stage)
        if meta and meta.get("kind") == "choice":
            return {"type": "options", "options": meta["options"]}
        if meta:
            return {"type": "text_input"}
        return {"type": "none"}

    def _schedule_ui(self):
        """Calendar+slot-picker payload for schedule_appointment - replaces
        the old plain typed-date text box. Computed fresh on every
        stage_update (not cached on the session) so a slot someone else
        just booked immediately shows as unavailable to a caller still
        picking, rather than only failing after they tap it."""
        today = datetime.now(ZoneInfo(config.CALENDAR_TIMEZONE)).date()
        max_date = today + timedelta(days=config.SCHEDULE_MAX_DAYS_AHEAD)
        booked = session_store.get_booked_slots_in_range(today.isoformat(), max_date.isoformat())
        kind = "visit" if self.session.slots.get("appointment_type") == "visit" else "callback"
        return {
            "type": "datetime",
            "prompt": f"What day and time works best for your {kind}?",
            "min_date": today.isoformat(),
            "max_date": max_date.isoformat(),
            "slot_minutes": config.SCHEDULE_SLOT_MINUTES,
            "business_start": config.SCHEDULE_BUSINESS_HOURS_START,
            "business_end": config.SCHEDULE_BUSINESS_HOURS_END,
            "booked": booked,
        }

    # ---- internals --------------------------------------------------------

    def _stage_meta(self, stage):
        if stage in S.STAGES:
            return S.STAGES[stage]
        if stage == "plan_choice":
            opts = [
                {
                    "value": p["id"],
                    "label": f'{p["name"]} - {p["price_display"]}',
                    "features": p.get("features", []),
                }
                for p in self.session.available_plans
            ]
            return {"kind": "choice", "options": opts, "prompt": ""}
        if stage == "plan_action":
            plans = self._chosen_plans()
            if plans:
                allowed = set(plans[0].get("actions_allowed") or [])
                for p in plans[1:]:
                    allowed &= set(p.get("actions_allowed") or [])
                if not allowed:
                    # Plans chosen together don't share a common action
                    # (e.g. different install crews) - "go with plan" is
                    # ambiguous for a bundle, but a human can always sort
                    # out a call or visit, so fall back to those two.
                    allowed = {"call", "visit"}
            else:
                allowed = {"go", "call", "visit"}
            action_map = [
                ("go", "go_with_plan", "Go With This Plan"),
                ("call", "arrange_call", "Arrange A Call"),
                ("visit", "arrange_visit", "Arrange A Visit"),
            ]
            options = [{"value": v, "label": lbl} for key, v, lbl in action_map if key in allowed]
            return {"kind": "choice", "prompt": "", "options": options}
        if stage == "call_timing":
            return {
                "kind": "choice",
                "prompt": "",
                "options": [
                    {"value": "immediate", "label": "Call me right now"},
                    {"value": "scheduled", "label": "Schedule a time"},
                ],
            }
        if stage == "schedule_appointment":
            kind = "visit" if self.session.slots.get("appointment_type") == "visit" else "callback"
            return {"kind": "text", "prompt": f"What day and time works best for your {kind} - today or tomorrow?"}
        if stage == "review_summary":
            return {
                "kind": "choice",
                "prompt": "",
                "options": [
                    {"value": "confirmed", "label": "Looks good, continue"},
                    {"value": "needs_changes", "label": "No, I need to change something"},
                ],
            }
        return None

    @staticmethod
    def _label(field, value):
        """Human-readable label for a stored choice value, e.g. 'attic_horizontal' -> 'Attic Horizontal'."""
        meta = S.STAGES.get(field)
        for opt in (meta or {}).get("options", ()):
            if opt["value"] == value:
                return opt["label"]
        return value or ""

    def _apply_value(self, stage, value):
        session = self.session
        session.slots[stage] = value
        session.voice_fail_count = 0
        logger.info("[%s] %s -> %s", session.session_id, stage, value)
        resume = session.resume_stage
        if resume and stage in PERSONAL_INFO_FIELDS:
            session.resume_stage = None
            session.stage = resume
            return self._entry_text(resume)
        if stage == "review_summary" and value != "confirmed":
            # Don't re-dump the whole summary - just ask what to fix.
            # Voice hears "what did I get wrong?"; text also sees the
            # Change-button hint since every answered field already has
            # one in the sidebar.
            session.stage = "review_summary"
            display = "No problem - what did I get wrong? You can tap Change next to any field, or just tell me."
            speech = "No problem - what did I get wrong?"
            return display, speech
        next_stage = self._advance(stage, value, resume)
        if next_stage in ("review_summary", "closing"):
            session.resume_stage = None  # correction resolved (or abandoned)
        session.stage = next_stage
        return self._entry_text(next_stage)

    def _advance(self, stage, value, resume=None):
        slots = self.session.slots
        if stage == "category":
            if value != "cooling_electric_heat":
                return "closing"
            if resume == "review_summary" and slots.get("tonnage") and slots.get("location"):
                return self._recheck_plans_or_ask("tonnage")
            return "tonnage"
        if stage == "tonnage":
            if resume == "review_summary" and slots.get("location"):
                return self._recheck_plans_or_ask("location")
            return "location"
        if stage == "location":
            plans = plan_matcher.get_available_plans(slots.get("category"), slots.get("tonnage"), value)
            self.session.available_plans = plans
            if not plans:
                return "closing"
            if resume == "review_summary" and self._plan_choice_still_valid(plans):
                return "review_summary"
            return "plan_choice"
        if stage == "plan_action":
            if value == "arrange_call":
                return "call_timing"
            if value == "arrange_visit":
                self.session.slots["appointment_type"] = "visit"
                return "schedule_appointment"
            self._submit_lead_once()
            return "closing"
        if stage == "call_timing":
            if value == "immediate":
                # BUG FIX ("gets stuck at immediately"): this used to call
                # trigger_immediate_call() inline and wait for it - it does
                # an SMTP send (and optionally a Twilio call) with up to a
                # 10s timeout each, after an up-to-10s WP-endpoint attempt,
                # all before this method could return "closing" and let the
                # bot speak. The caller heard dead air (or a stuck UI) for
                # however long that chain took. The closing line doesn't
                # depend on the result, so fire it in the background and
                # return immediately.
                slots = self.session.slots

                def _run():
                    slots["_call_result"] = call_service.trigger_immediate_call(self.session)

                threading.Thread(target=_run, daemon=True).start()
                self.session.slots["_lead_submitted"] = True  # trigger_immediate_call already sends it (urgent)
                return "closing"
            self.session.slots["appointment_type"] = "call"
            return "schedule_appointment"
        if stage == "review_summary" and value != "confirmed":
            # "No, I need to change something" - stay right here. The
            # caller can now either say what to fix (voice/text, handled
            # by go_back_or_edit) or tap any field's "Change" button,
            # which works standalone with zero model involvement.
            return "review_summary"
        if stage == "schedule_appointment":
            self._submit_lead_once()
            return "closing"
        if stage not in S.ORDER:
            # Defensive: never crash the FSM on an unexpected/terminal
            # stage (e.g. a stray tool call after 'closing' slipping
            # past the caller-side guard in realtime_tools.py) - just
            # stay put instead of raising ValueError.
            return stage
        idx = S.ORDER.index(stage)
        return S.ORDER[idx + 1]

    def _recheck_plans_or_ask(self, ask_stage):
        """Called when tonnage/category is corrected during review_summary.
        Tries the caller's EXISTING location (and plan, if any) against the
        new tonnage/category first - only falls back to re-asking `ask_stage`
        if that combo genuinely has no plans. This is what stops a tonnage
        correction from forcing an unnecessary re-ask of location."""
        slots = self.session.slots
        plans = plan_matcher.get_available_plans(slots.get("category"), slots.get("tonnage"), slots.get("location"))
        self.session.available_plans = plans
        if not plans:
            return ask_stage
        if self._plan_choice_still_valid(plans):
            return "review_summary"
        return "plan_choice"

    def _plan_choice_still_valid(self, plans):
        pid = self.session.slots.get("plan_choice")
        ids = pid if isinstance(pid, list) else ([pid] if pid else [])
        valid_ids = {p["id"] for p in plans}
        return bool(ids) and all(i in valid_ids for i in ids)

    def _submit_lead_once(self):
        """Single source of truth for 'have we emailed/webhooked this
        lead yet' - both _advance's own transitions AND the S2S
        save_lead_to_db tool (services/realtime_tools.py) check this
        same flag, so a lead can never get double-sent regardless of
        which path reaches closing first. Backgrounded for the same reason
        as trigger_immediate_call above - notify.submit_lead's WP-endpoint
        + SMTP chain can take 10-20s+, and this used to block the closing
        line on it."""
        if not self.session.slots.get("_lead_submitted"):
            threading.Thread(target=notify.submit_lead, args=(self.session,), daemon=True).start()
            self.session.slots["_lead_submitted"] = True

    def _entry_text(self, stage):
        if stage == "closing":
            text = self._closing_text()
            return text, text
        if stage == "plan_choice":
            plans = self.session.available_plans
            n = len(plans)
            plural = "s" if n != 1 else ""
            desc = "; ".join(f"{p['name']} at {p['price_display']}" for p in plans)
            display = f"I found {n} option{plural} for you. Which would you like?"
            speech = f"I found {n} option{plural}: {desc}. Which would you like?"
            return display, speech
        if stage == "plan_action":
            plans = self._chosen_plans()
            name = " and ".join(p["name"] for p in plans) if plans else "that plan"
            text = f"Great choice, {name}. Would you like to go with this plan, arrange a call, or arrange a visit?"
            return text, text
        if stage == "call_timing":
            text = "Would you like a call right now, or would you prefer to schedule a time?"
            return text, text
        if stage == "schedule_appointment":
            kind = "visit" if self.session.slots.get("appointment_type") == "visit" else "callback"
            text = f"What day and time works best for your {kind} - today or tomorrow, and what time?"
            return text, text
        if stage == "review_summary":
            s = self.session.slots
            plans = self._chosen_plans()
            plan_desc = " and ".join(p["name"] for p in plans) if plans else "no plan selected"
            address = ", ".join(p for p in (s.get("street"), s.get("city"), s.get("zip")) if p)
            lines = [
                "Here's what I have:",
                f"Full Name: {s.get('full_name', '')}",
                f"Phone: {s.get('phone', '')}",
                f"Email: {s.get('email', '')}",
                f"Address: {address}",
                f"System: {self._label('category', s.get('category'))}, "
                f"{self._label('tonnage', s.get('tonnage'))}, "
                f"{self._label('location', s.get('location'))}",
                f"Plan: {plan_desc}",
                "Does everything look right, or would you like to change something?",
            ]
            text = "\n".join(lines)
            return text, text
        meta = S.STAGES.get(stage)
        if meta:
            speech = meta["prompt"].format(**self.session.slots)
            display = meta.get("display_prompt", meta["prompt"]).format(**self.session.slots)
            return display, speech
        text = "Let's continue."
        return text, text

    def _chosen_plan(self):
        return next(iter(self._chosen_plans()), None)

    def _chosen_plans(self):
        """plan_choice is a list of plan ids now (multi-select support) -
        this resolves them all against available_plans. Old sessions
        where plan_choice was saved as a single string still work
        (wrapped into a 1-item list)."""
        pid = self.session.slots.get("plan_choice")
        ids = pid if isinstance(pid, list) else ([pid] if pid else [])
        return [p for p in self.session.available_plans if p["id"] in ids]

    def _closing_text(self):
        slots = self.session.slots
        category = slots.get("category")
        if category in ("heating", "cooling_heat_pump"):
            return ("Thanks! We don't have instant plans for that category yet, but I've "
                    "passed your details to our team and they'll reach out shortly.")
        if not self.session.available_plans:
            return ("I'm sorry, we don't have a plan for that exact setup right now. "
                    "I've passed your details to our team and someone will follow up directly.")
        name = (slots.get("full_name", "") or "").split(" ", 1)[0]

        if slots.get("call_timing") == "immediate":
            return f"You're all set, {name}! Our team has been notified and will be calling you right away."

        if slots.get("appointment_date"):
            kind = "visit" if slots.get("appointment_type") == "visit" else "callback"
            when = f"{slots['appointment_date']} at {slots.get('appointment_time', '')}"
            if slots.get("_calendar_attendee_invited"):
                reminder = "You'll get a calendar reminder, and so will our team."
            else:
                # No customer-facing calendar invite actually went out
                # (e.g. domain-wide delegation isn't configured yet) -
                # don't promise an email that was never sent.
                reminder = "Our team has it on their calendar and will reach out to confirm."
            return f"You're all set, {name}! Your {kind} is booked for {when}. {reminder}"

        action_text = {
            "go_with_plan": "confirm your plan",
            "arrange_call": "arrange your call",
            "arrange_visit": "arrange your visit",
        }.get(slots.get("plan_action"), "follow up")
        return f"You're all set, {name}! An email is on its way and our team will be in touch shortly to {action_text}."

    def restart(self):
        """Resets the whole session back to the first question, keeping
        the same session_id. Used by 'start over'/'restart' voice intent
        and reused directly by the S2S go_back_or_edit tool."""
        self.session.slots = {}
        self.session.partial_inputs = {}
        self.session.available_plans = []
        self.session.resume_stage = None
        self.session.stage = "full_name"
        text = "No problem, let's start fresh. " + S.STAGES["full_name"]["prompt"]
        return text, text

    def _check_meta_intent(self, transcript, stage):
        t = transcript.lower()
        if not t:
            return None
        if "start over" in t or "restart" in t:
            return self.restart()
        if "go back" in t or "previous" in t:
            display, speech = self.go_back()
            return "Sure, going back. " + display, "Sure, going back. " + speech
        if "repeat" in t or "say that again" in t or "what did you say" in t:
            return self._entry_text(stage)
        edit_field = _find_edit_field(t)
        if edit_field:
            if edit_field not in self.session.slots:
                text = "We haven't gotten to that question yet - let's finish this one first."
                return text, text
            display, speech = self.jump_to(edit_field)
            return "No problem. " + display, "No problem. " + speech
        return None