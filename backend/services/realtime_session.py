"""
Persistent WebSocket session manager for the S2S path (the only voice
path on this branch).

One RealtimeSession is created per live call (see main.py's
/ws/realtime/{session_id} endpoint) and owns a single long-lived
connection to OpenAI's Realtime API for the whole conversation - one
continuous stream, no per-turn request/response cycle.

Untested against the live OpenAI endpoint in this environment (no
outbound network access to api.openai.com from this sandbox) - the event
names, session.update shape, and function-calling flow below follow
OpenAI's published Realtime API docs as of this writing. Before trusting
this in production: run it against a real OPENAI_API_KEY and confirm the
event names/payload shapes against the current docs, since Realtime is a
newer, faster-moving API surface than the plain chat completions one.

Caching note: session.update is a PARTIAL update - fields you omit are
left as-is server-side. The static instructions block below is sent
exactly ONCE, at connection time, and never touched again for the rest
of the call; every stage transition only sends an updated `tools` list.
This is what actually lets OpenAI's automatic prompt caching apply to the
(much larger) instructions block on every single turn after the first -
resending the full instructions text on every stage change, like an
earlier version of this file did, would invalidate that cached prefix
every time a stage changed instead of only once per call.
"""
import asyncio
import time
import base64
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import websockets
from fastapi import WebSocketDisconnect

import config
from dialogue import slots as S
from dialogue.state_machine import DialogueManager
from services import realtime_tools, session_store

logger = logging.getLogger("realtime_session")


_BASE_INSTRUCTIONS = "\n".join([
    "You are a phone voice assistant collecting an HVAC service quote for "
    "a home services company. Follow the stages in order, one question at "
    "a time. Do not skip ahead or ask about fields not yet reached.",
    "",
    "STRICT RULES (do not deviate):",
    "- Call confirm_slot(field, value) as soon as you have a clear answer "
    "for the CURRENT stage's field. Do NOT read the value back and ask "
    "the caller to confirm it first - that per-field confirmation is "
    "handled once, in bulk, at review_summary later, so doing it again "
    "here just wastes the caller's time and doubles the call length. "
    "It only works for the CURRENT stage's field - it will reject "
    "anything else. If the answer is genuinely unclear or inaudible, "
    "ask them to repeat it - don't guess and don't confirm a guess. "
    "ANY time you ask the caller to repeat, clarify, or disambiguate "
    "something (including street/city names that sound like they might "
    "be a neighborhood, partial address, or anything else uncertain), "
    "use ONE short sentence, max ~10 words (e.g. 'Sorry, can you say "
    "that again?' or 'Sorry, what was the full address?'). Do NOT "
    "restate what you heard back in quotes, do NOT explain why it "
    "seemed ambiguous, do NOT give multi-option instructions on how to "
    "answer - every one of those is dead air on a phone call and makes "
    "the bot sound unsure. This applies every time, not just the first.",
    "- For a CHOICE-type field (category, tonnage, location, plan_choice, "
    "plan_action, call_timing): if your turn ends on only a filler word "
    "('I', 'um', 'so') or anything short of a real option being named, "
    "that is NOT a clear answer - do not call confirm_slot at all, and "
    "do NOT default to the first-listed option. Ask 'Sorry, which one "
    "did you mean?' and wait for the real answer instead.",
    "- Say NOTHING before calling confirm_slot either - no 'okay', no "
    "'let me think about that', no 'got it, one moment'. The instant you "
    "have a clear answer, call the tool immediately with zero spoken "
    "words first. Any filler here delays your own tool call behind a "
    "full sentence of generated speech, which is pure added wait for "
    "the caller for no benefit - say_next already gives you the next "
    "line to speak, right after.",
    "- The caller is speaking English. For plainly spoken answers, pass "
    "confirm_slot their words in normal spelling - never invent a "
    "phonetic breakdown of how a name sounds (not 'Sha-riq', not "
    "'Saa-rik' - just 'Shariq'). Only when the caller explicitly spells "
    "something out letter-by-letter or gives a correction ('with a q', "
    "'no, k not c') should you pass those exact letters/words verbatim, "
    "unmodified - a deterministic step downstream applies that "
    "correction, but only from your literal transcription of it.",
    "- The instant you call confirm_slot, stop - say NOTHING else in "
    "that same turn. No acknowledgment, no 'let me check that', no "
    "guess about what happens next. You will be told the exact next "
    "line to speak via say_next in a separate turn immediately after - "
    "inventing your own line instead of waiting for it is the single "
    "most common way this call goes wrong, because your guess about "
    "plans/pricing/what's next is frequently just incorrect.",
    "- Call confirm_slot at most ONCE per caller turn. If a tool result "
    "is an error (e.g. 'not the current question'), that means you "
    "already called it once this turn and the field moved on - do NOT "
    "explain the error to the caller, do NOT re-ask the question "
    "yourself, and do NOT call it again. Just wait silently for the "
    "next say_next you were already given.",
    "- When a tool result includes a say_next value, that is the exact "
    "next thing to say - use it as-is, don't paraphrase numbers or plan "
    "names out of it. This is the ONLY place you learn the next question, "
    "so always check for it after every tool call.",
    "- The caller can choose more than one plan at plan_choice - if they "
    "want two, pass both ids comma-separated in one confirm_slot call "
    "(e.g. value='cooling_better,heating_better'), don't call it twice.",
    "- If the caller wants to go back, change an earlier answer, hear the "
    "question again, or start over, call go_back_or_edit instead of "
    "forcing it through confirm_slot.",
    "- Phone numbers may arrive in more than one turn if the caller pauses "
    "partway through - pass whatever digits you heard to "
    "confirm_slot(field='phone', value=<those digits>) each time; it will "
    "tell you via say_next whether to ask for more digits or whether the "
    "number is complete. This applies EVERY time the caller says any "
    "digits for this field, including a retry after 'that didn't sound "
    "like 10 digits' or after being asked for more digits - always call "
    "confirm_slot with whatever digits you just heard, never just ask a "
    "follow-up question yourself without calling it.",
    "- CRITICAL for phone/zip digits: count each spoken digit "
    "individually, one at a time, especially REPEATED digits ('eight "
    "eight', 'five five', 'double eight'). A repeated digit means TWO "
    "separate digits in the value, not one - do not collapse them. "
    "Before calling confirm_slot with digits, count them back to "
    "yourself one at a time to make sure none were merged.",
    "- If the caller chooses 'arrange a call', you'll reach a "
    "call_timing question next - ask if they want a call right now or "
    "a scheduled time, then confirm_slot(field='call_timing', "
    "value='immediate' or 'schedule'). Choosing 'immediate' notifies "
    "the team to call right away, no date/time needed. Choosing "
    "'schedule', or choosing 'arrange a visit' at plan_action, leads to "
    "a schedule_appointment stage - ask what date and time works and "
    "accept however the caller says it, don't restrict them to only "
    "'today' or 'tomorrow'. Resolve whatever they say into an exact "
    "YYYY-MM-DD yourself before calling the tool: relative terms "
    "('today', 'tomorrow') compute from the current date given to you "
    "above; a bare weekday name ('Sunday') with no other qualifier means "
    "the NEAREST occurrence on or after today - today itself if today "
    "already is that weekday, otherwise the very next one, and only the "
    "FOLLOWING week if they explicitly say 'next Sunday'; an explicit "
    "date ('15 July', 'July 15th 2026') converts directly. Say the "
    "resolved date back in plain words ('so that's Sunday the 14th at "
    "8 PM') before calling the tool, so a misread date gets caught. "
    f"Bookings are only accepted within the next {config.SCHEDULE_MAX_DAYS_AHEAD} "
    "days - if the caller names something further out, tell them that "
    "and ask for a closer date. Then call the schedule_appointment tool "
    "with the resolved call_date/call_time - if it comes back as an "
    "error (slot taken, or date out of range), explain why and offer a "
    "different time.",
    "- Never state a dollar price or plan name unless it came from a "
    "confirm_slot or get_plan_pricing tool result earlier in THIS "
    "conversation - never from memory or estimation.",
    "- After plan_choice you'll reach review_summary: say_next will read back "
    "every answer plus the chosen plan(s) and ask if it's all correct. Do not "
    "skip or shorten this - read it exactly as given. If the caller confirms, "
    "call confirm_slot(field='review_summary', value='confirmed'). If they "
    "want to fix something (even several things in one sentence, e.g. 'my "
    "phone is X and make it 2.5 tons'), handle each correction in turn with "
    "go_back_or_edit(action='edit_field', field=...) then confirm_slot for "
    "that field, before moving on to the next correction - you'll land back "
    "on an updated review_summary automatically once all corrections are in.",
    "- Any message wrapped in [System note: ...] is not something the "
    "caller said - it's a record of a button they tapped on screen. "
    "It's already saved. Never call confirm_slot (or any tool) in "
    "reaction to one, even if it mentions a field/value by name.",
    "- If the caller talks over you, stop immediately and listen.",
    "- If an answer is unclear, ask them to repeat it rather than guessing. "
    "This matters most for full_name, street, and city - none of these are "
    "checked against a fixed list, so nothing downstream catches a wrong "
    "guess. An uncommon or non-English name/place is exactly where guessing "
    "goes wrong - if it doesn't sound like a clearly, confidently heard "
    "answer, ask the caller to repeat or spell it rather than substituting "
    "something plausible-sounding.",
    "- Once a tool result's 'stage' is 'closing', the call is complete: "
    "say the say_next text and then STOP calling any tools at all, even "
    "if the caller says something else afterward. There is nothing left "
    "to confirm, edit, or save at that point.",
    "",
    f"Start the call now: warmly greet the caller, then ask - {S.STAGES['full_name']['prompt']}",
])


def turn_detection_config():
    """Standalone copy of RealtimeSession._turn_detection_config so the
    WebRTC config endpoint (main.py) can build an identical session dict
    without a live RealtimeSession instance. Keep these two in sync if
    VAD behavior ever changes - see the method version's own comments
    for the reasoning behind each field."""
    if config.REALTIME_VAD_TYPE == "semantic_vad":
        return {
            "type": "semantic_vad",
            "eagerness": config.REALTIME_VAD_EAGERNESS,
            "create_response": False,
            "interrupt_response": False,
        }
    return {
        "type": "server_vad",
        "threshold": config.REALTIME_VAD_THRESHOLD,
        "silence_duration_ms": config.REALTIME_VAD_SILENCE_MS,
        "create_response": False,
        "interrupt_response": False,
    }


def build_session_dict(session):
    """The "session" object of a session.update event - identical shape
    whether sent over the legacy server-relayed WebSocket
    (_send_initial_session_update below) or straight from the browser
    over a WebRTC data channel (see main.py's realtime-session-config
    endpoint, used by the WebRTC frontend). Kept as one function so the
    two transports can never drift apart."""
    tools = realtime_tools.tools_for_stage(session)
    now = datetime.now(ZoneInfo(config.CALENDAR_TIMEZONE))
    date_line = f"\n\nToday's real date is {now.strftime('%Y-%m-%d')} ({now.strftime('%A')}). Compute 'today'/'tomorrow' for scheduling from this, don't guess."
    return {
        "type": "realtime",
        "model": config.REALTIME_MODEL,
        "output_modalities": ["audio"],
        "instructions": _BASE_INSTRUCTIONS + date_line,
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "noise_reduction": {"type": config.REALTIME_VAD_MODE},
                "transcription": {"model": "gpt-4o-transcribe", "language": "en"},
                "turn_detection": turn_detection_config(),
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "voice": config.REALTIME_VOICE,
                "speed": 1.2,
            },
        },
        "tools": tools,
        "tool_choice": "auto",
        "reasoning": {"effort": "low"},
        "truncation": {
            "type": "retention_ratio",
            "retention_ratio": 0.8,
            "token_limits": {"post_instructions": 6000},
        },
    }


GREETING_INSTRUCTIONS = (
    f"Say exactly, word for word, nothing before or after: {S.STAGES['full_name']['prompt']}"
)


class RealtimeSession:
    """Owns one call's connection to OpenAI's Realtime API and relays
    audio + tool calls to/from the caller's browser WebSocket."""

    def __init__(self, session, client_ws):
        self.session = session
        self.client_ws = client_ws  # FastAPI WebSocket to the caller's browser
        self.oa_ws = None
        self._last_activity = time.monotonic()
        self._call_ending = False       # True once session.stage == "closing"
        self._call_ending_at = None     # monotonic time closing was first seen
        self._call_end_reason = None
        self._input_audio_bytes = 0     # tallies caller audio streamed - see log_whisper_audio_bytes
        # BUG FIX: this app has 6 different triggers that can each want to
        # start a response (tool calls, button clicks, typed text,
        # push-to-talk, server VAD) - firing response.create while one is
        # already in flight gets rejected by OpenAI with
        # conversation_already_has_active_response. Track it and queue
        # instead of firing blind - see _create_response().
        self._response_active = False
        self._pending_responses: list[tuple[str | None, str]] = []  # (instructions, for_stage)
        self._audio_bytes_forwarded = 0
        self._response_create_sent_at = None
        self._response_first_audio_at = None
        self._response_kind = None
        self._response_for_stage = None
        # BUG FIX: _handle_tool_call (driven by the model's own tool calls,
        # via _pump_openai_to_client) and _handle_client_control's
        # select_option/go_back/edit_field branches (driven by button taps,
        # via _pump_client_to_openai) both read-mutate-send session.stage,
        # and each awaits multiple times (oa_ws.send, _create_response,
        # client_ws.send_json) in between. Since those two coroutines run
        # as separate tasks, one can fully run its own stage mutation
        # during the other's await gap - e.g. a queued/stale click landing
        # mid-flight through a model-driven tool call. That's what was
        # producing "impossible" log sequences like a go_back logging
        # `at stage=tonnage` right after a confirm_slot(tonnage,...) had
        # already advanced the stage to `location` with no go_back/edit
        # call in between. Every stage-mutating handler below now holds
        # this lock for its full mutate-then-notify-client sequence, so
        # the two tasks can never interleave mid-transition.
        self._stage_lock = asyncio.Lock()
        # Audio-echo self-interrupt protection already lives client-side
        # (realtime-widget.js's `micMuted`, which correctly excludes
        # push-to-talk mode). A server-side mute was tried here and
        # reverted - it didn't know about the push-to-talk exception and
        # silently dropped real held-button audio, producing
        # input_audio_buffer_commit_empty. Don't re-add muting here
        # without also respecting push-to-talk.
        self._bytes_since_last_commit = 0  # see push_to_talk_commit guard below
        # BUG FIX: tracks bytes streamed since the current VAD turn opened,
        # so speech_stopped (below) can tell a real spoken answer apart
        # from a noise/echo blip that briefly crossed the client-side VAD
        # gate - see the guard in speech_stopped.
        self._bytes_since_speech_started = 0
        # BUG FIX: counts VAD blips we decided to ignore (see
        # speech_stopped) so the matching Whisper aux-transcript event -
        # which fires independently and isn't gated by that check - can
        # be dropped too instead of showing hallucinated captions like
        # "Bye." or "Thank you." for noise that was never really said.
        self._ignored_blip_count = 0
        # Set the instant a bot response finishes and nothing else is
        # queued (i.e. we're now genuinely waiting on the caller). Cleared
        # the instant a real response.create actually sends. See _watchdog
        # and config.REALTIME_STUCK_TURN_SECONDS.
        self._awaiting_caller_since: float | None = None
        # Set on every input_audio_buffer.speech_started, used by the
        # watchdog to tell "caller just started talking" apart from
        # "genuinely stuck VAD" - see _watchdog.
        self._speech_started_at: float | None = None
        # BUG FIX (confirm_slot turns still growing every call): tracks the
        # last few conversation item ids as they're created server-side
        # (see conversation.item.created below) so the bare-call branch of
        # _create_response() can bound its input to just the most recent
        # exchange instead of the whole call so far - same fix as the
        # scripted-turn input:[] one just above it, but these turns
        # genuinely need to hear real audio so it can't be an empty array.
        # REMOVED (2026-07-14): this used to track self._recent_item_ids and
        # bound confirm-turns' response.create to item_reference(last 4
        # items) as a cost/latency optimization. Real call logs proved it
        # never worked - referencing an item_reference apparently pulls in
        # everything up to that item's position in the conversation anyway
        # (uncached input/audio tokens grew turn-over-turn identically to
        # the unbounded default, see usage log from 2026-07-14 15:33 call).
        # It was pure downside: zero cost benefit, plus a real ordering bug
        # (item_reference computed before the just-committed item's ack
        # arrived) that let confirm_slot fire against STALE references
        # missing the caller's actual answer - the "hallucinated field"
        # bug. Bare confirm-turns now just use the default conversation
        # (no override), same as they did before that optimization existed.
        # If context cost ever needs bounding for real, it has to be done
        # via directly-embedded input content + "conversation": "none" -
        # NOT item_reference - and tested against the live API first (this
        # sandbox has no network path to api.openai.com to verify that).

    async def run(self):
        if not config.OPENAI_API_KEY:
            await self.client_ws.send_json(
                {"type": "error", "message": "OPENAI_API_KEY is not configured on the server."}
            )
            return

        # GA Realtime API - the old `realtime=v1` beta shape was retired;
        # OpenAI now rejects connections carrying the
        # `OpenAI-Beta: realtime=v1` header with 4000
        # invalid_request_error.beta_api_shape_disabled. GA needs no beta
        # header at all - plain bearer auth.
        url = f"{config.REALTIME_WS_URL}?model={config.REALTIME_MODEL}"
        headers = [
            ("Authorization", f"Bearer {config.OPENAI_API_KEY}"),
        ]
        try:
            # NOTE: `extra_headers` was the kwarg name through websockets
            # 12.x/13.x; it was renamed to `additional_headers` and removed
            # entirely in websockets 14+. requirements.txt now pins
            # `websockets>=14.0` specifically so `additional_headers` below
            # is always valid - on 12.x/13.x this call would raise
            # TypeError before ever reaching OpenAI, caught by the except
            # below and reported to the caller as a generic "connection
            # failed".
            async with websockets.connect(url, additional_headers=headers, max_size=None) as oa_ws:
                self.oa_ws = oa_ws
                await self._send_initial_session_update()
                await self.client_ws.send_json({"type": "connected"})
                # BUG FIX: session.update only configures the session - it
                # never makes the model speak. Nothing was ever asking for
                # a first response, so the "warmly greet the caller" line
                # in _BASE_INSTRUCTIONS was silently never spoken; the
                # first thing callers ever heard was whatever answered
                # their first typed/spoken turn. Kick off the greeting
                # explicitly, exactly once, right after connecting.
                await self._create_response(instructions=GREETING_INSTRUCTIONS)

                pump_in = asyncio.create_task(self._pump_client_to_openai())
                pump_out = asyncio.create_task(self._pump_openai_to_client())
                watchdog = asyncio.create_task(self._watchdog())
                done, pending = await asyncio.wait(
                    {pump_in, pump_out, watchdog}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                if self._call_end_reason:
                    logger.info("[%s] call ended: %s", self.session.session_id, self._call_end_reason)
                    try:
                        await self.client_ws.send_json({"type": "call_ended", "reason": self._call_end_reason})
                    except Exception:
                        pass
                try:
                    await self.client_ws.close()
                except Exception:
                    pass
        except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
            logger.info("[%s] client disconnected before/during setup", self.session.session_id)
        except Exception:
            logger.exception("[%s] realtime session failed", self.session.session_id)
            try:
                await self.client_ws.send_json(
                    {"type": "error", "message": "Voice engine connection failed. Try push-to-talk fallback."}
                )
            except Exception:
                pass
        finally:
            session_store.save_progress(self.session, is_complete=self.session.stage == "closing")
            session_store.log_whisper_audio_bytes(self.session.session_id, self._input_audio_bytes)

    async def _watchdog(self):
        """Runs alongside the two relay pumps. Whichever of the three
        finishes first ends the call (see run()) - this is what actually
        hangs up the OpenAI connection instead of leaving it billing
        per-minute forever. Three independent reasons to end here:
        1. Reached "closing" and its spoken line has had time to finish.
        2. No audio/event activity from either side for a while (caller
           went silent and never hung up, or the browser tab died).
        3. Absolute hard cap, regardless of activity - a backstop in
           case (1)/(2) ever miss a case.
        """
        start = time.monotonic()
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            if self._call_ending and self._call_ending_at is not None and (now - self._call_ending_at) >= config.REALTIME_CALL_END_GRACE_SECONDS:
                self._call_end_reason = "completed"
                return
            if (now - start) >= config.REALTIME_MAX_CALL_SECONDS:
                self._call_end_reason = "max_duration"
                return
            if (now - self._last_activity) >= config.REALTIME_IDLE_TIMEOUT_SECONDS:
                self._call_end_reason = "idle"
                return
            # BUG FIX: semantic_vad can, in practice, just never decide the
            # caller is done talking (ambient noise keeping it "open",
            # audio dropping a frame, etc). Audio still streams in the
            # whole time, so _last_activity keeps getting bumped and the
            # idle check above never trips - the call just hangs until the
            # caller gives up and clicks a UI button. If we've been
            # waiting on the caller this long with no speech_stopped and
            # no response in flight, force OpenAI to commit whatever's in
            # its buffer and respond to it now, rather than waiting
            # indefinitely for a turn boundary that may never come.
            if (
                self._awaiting_caller_since is not None
                and not self._response_active
                and (now - self._awaiting_caller_since) >= config.REALTIME_STUCK_TURN_SECONDS
            ):
                recent_speech_onset = (
                    self._speech_started_at is not None
                    and (now - self._speech_started_at) < 2.0
                )
                if recent_speech_onset:
                    # BUG FIX: caller just started talking within the last
                    # 2s - forcing a commit right now truncates them
                    # mid-word. This is exactly what corrupted city -> "Une"
                    # in testing: the caller had just begun saying "Pune"
                    # when this fired, the model got a clipped partial
                    # buffer, misheard it, and confirmed the wrong value
                    # before the real transcript ("Pune") even arrived.
                    # Don't touch _awaiting_caller_since - just skip this
                    # tick and re-check next second, either speech_stopped
                    # closes the turn normally or the 2s window clears.
                    pass
                elif self._bytes_since_last_commit == 0:
                    # No real audio has streamed in since we started
                    # waiting - the caller is just slow to start talking,
                    # not actually VAD-stuck. Forcing a commit fed OpenAI a
                    # genuinely empty buffer, and since we still ask it to
                    # respond, the model hallucinates an answer instead of
                    # erroring cleanly - this is what corrupted full_name
                    # in testing. Just restart the window and keep waiting;
                    # the idle timeout above still ends the call if the
                    # caller never speaks at all.
                    self._awaiting_caller_since = now
                else:
                    logger.info(
                        "[%s] no turn boundary for %.1fs at stage=%s - forcing a manual commit",
                        self.session.session_id, now - self._awaiting_caller_since, self.session.stage,
                    )
                    self._awaiting_caller_since = None
                    self._bytes_since_last_commit = 0
                    try:
                        await self.oa_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    except Exception:
                        logger.exception("[%s] forced commit failed", self.session.session_id)
                    else:
                        await self._create_response()

    def _note_activity(self):
        self._last_activity = time.monotonic()

    def _note_stage_for_call_ending(self):
        """Call right after any tool call/action that may have moved
        session.stage - flips the closing flag exactly once, so the
        watchdog's grace period starts counting from the first time we
        reach "closing", not from every subsequent stage_update."""
        if self.session.stage == "closing" and not self._call_ending:
            self._call_ending = True

    # ---- OpenAI session config ------------------------------------------

    def _turn_detection_config(self):
        return turn_detection_config()

    async def _send_initial_session_update(self):
        """Sent exactly once per call. Delegates to build_session_dict so
        this stays byte-identical to what the WebRTC path sends over its
        data channel - see that function's docstring."""
        await self.oa_ws.send(json.dumps({
            "type": "session.update",
            "session": build_session_dict(self.session),
        }))

    async def _refresh_tools_for_stage(self):
        """Called whenever confirm_slot/go_back_or_edit change
        session.stage. Deliberately sends ONLY `tools` - omitting
        `instructions` leaves the cached static block untouched server-
        side instead of re-billing/re-caching it on every stage change."""
        tools = realtime_tools.tools_for_stage(self.session)
        await self.oa_ws.send(json.dumps({
            "type": "session.update",
            "session": {"type": "realtime", "tools": tools},
        }))

    # ---- relay loops ------------------------------------------------------

    async def _pump_client_to_openai(self):
        """Browser -> OpenAI: raw PCM16 mic chunks streamed continuously,
        see frontend/realtime-widget.js. No per-turn record/stop cycle -
        server_vad on the OpenAI side decides where turns start/end."""
        try:
            while True:
                message = await self.client_ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                self._note_activity()
                data = message.get("bytes")
                if data is None:
                    text = message.get("text")
                    if text:
                        await self._handle_client_control(json.loads(text))
                    continue
                self._input_audio_bytes += len(data)
                self._bytes_since_last_commit += len(data)
                self._bytes_since_speech_started += len(data)
                await self.oa_ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(data).decode(),
                }))
        except Exception:
            logger.info("[%s] client audio stream ended", self.session.session_id)

    async def _create_response(self, instructions: str | None = None):
        """The only place that should ever send response.create. If a
        response is already active, queues this one instead of firing it
        - it fires automatically from response.done once the active one
        finishes. Queued entries are tagged with session.stage AT QUEUE
        TIME so response.done can tell a stale scripted line (the caller
        already clicked/answered past it before it got a turn to speak)
        from a still-current one - see the staleness check there. This
        is what stops rapid option-card clicking in hands-free mode from
        producing a bot that's always one question behind what's on
        screen."""
        if self._response_active:
            self._pending_responses.append((instructions, self.session.stage))
            return
        self._response_active = True
        self._awaiting_caller_since = None
        self._response_create_sent_at = time.monotonic()
        self._response_first_audio_at = None
        self._response_kind = "scripted" if instructions else "bare"
        self._response_for_stage = self.session.stage
        payload = {"type": "response.create"}
        if instructions:
            # BUG FIX: every call site that passes `instructions` is a
            # scripted "Say exactly, word for word: ..." line straight from
            # the FSM's say_next - the model is never supposed to do
            # anything else on this turn. Without tool_choice="none" it
            # still inherits the session-level "auto" and can (and did)
            # opportunistically call confirm_slot with a guessed value
            # instead of just reciting the line - see e.g. tonnage getting
            # silently confirmed as '3_ton' half a second after category
            # was answered, with no question ever spoken and no time for
            # the caller to have said anything. Locking tool_choice here
            # makes that structurally impossible instead of just unlikely.
            # BUG FIX (turns getting slower deeper into the call): every
            # response.create against the default conversation reprocesses
            # the FULL conversation so far as input - that's why input_tokens
            # (and latency) climbs turn over turn as the call goes on. These
            # scripted lines don't need any of that history to know what to
            # say - the text is already fully decided in Python - so give
            # the model an empty input array. Per OpenAI's docs this still
            # inserts the response into the default conversation (later
            # turns still see it was said) but this turn itself is generated
            # from `instructions` alone, not the growing transcript, so its
            # cost stays flat whether it's question 1 or question 20.
            payload["response"] = {"instructions": instructions, "tool_choice": "none", "input": []}
            # NOTE: the confirm-turn (bare, no instructions) case used to
            # have an elif here bounding input to item_reference(last 4
            # items) as the same kind of cost fix. Removed 2026-07-14 -
            # real call logs proved it never actually bounded anything
            # (input/audio tokens grew turn-over-turn identically with or
            # without it) while adding a real ordering bug that let
            # confirm_slot fire against stale references missing the
            # caller's actual answer. Bare calls now just use the default
            # conversation, unmodified, same as before that "fix" existed.
        await self.oa_ws.send(json.dumps(payload))

    async def _inject_user_note(self, text: str):
        """Records a non-spoken user action (option tap, Back, Change) as
        an ordinary user turn in the live OpenAI conversation, WITHOUT
        asking for a response to it (no response.create follows - the
        caller of this always sends its own scripted _create_response
        right after).

        This is the actual fix for clicks derailing the call: select_option/
        go_back/edit_field used to mutate session.stage and speak a scripted
        line WITHOUT ever telling OpenAI anything happened. From the model's
        own conversation history that looked like several assistant turns
        in a row with no caller reply in between - it never "heard" category/
        tonnage/location get answered, just its own voice asking about them
        one after another. That's exactly what was producing the mid-call
        regression (model suddenly re-asking tonnage after location had
        already been tapped): the moment ANY unscripted turn fired (see
        response.done below), the model had nothing coherent to work from
        and free-associated back to an earlier question instead."""
        await self.oa_ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }))

    async def _handle_client_control(self, msg: dict):
        mtype = msg.get("type")

        def ui_payload():
            return DialogueManager(self.session).ui_for_stage()

        if mtype == "push_to_talk_commit":
            # Push-to-talk fallback mode: frontend disables server VAD
            # locally and explicitly tells us when the caller's turn is
            # done, instead of relying on far-field VAD.
            if self._bytes_since_last_commit == 0:
                # Nothing was actually captured (accidental/very brief
                # tap) - committing an empty buffer errors server-side,
                # and firing a response on zero audio just produces a
                # reply disconnected from anything the caller said.
                return
            self._bytes_since_last_commit = 0
            await self.oa_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await self._create_response()
            return

        if mtype == "go_back":
            # Header/inline "Back" button, ported from the cascaded
            # widget.js's makeBackButton(). Same direct-call pattern as
            # edit_field/replay: a click is unambiguous, no reason to
            # make the model decide to call go_back_or_edit itself.
            # BUG FIX: locked so this click's mutate-then-notify sequence
            # can't interleave with a concurrent model-driven tool call
            # from _handle_tool_call - see the lock's docstring in __init__.
            async with self._stage_lock:
                await self._inject_user_note("[System note: caller tapped Back - already handled, no tool call needed.]")
                # BUG FIX (latency): call_tool can end in a blocking network
                # call (notify.submit_lead's smtplib send, Twilio's
                # client.calls.create, or Google Calendar's execute() - see
                # call_service.py/calendar_service.py/notify.py, none of
                # which are async). Calling it directly here froze this
                # whole process's asyncio event loop for the duration of
                # that network call - not just this caller's turn, but
                # every other concurrent call's audio pump too. Running it
                # in a worker thread via asyncio.to_thread fixes that
                # without touching any of the business-logic modules -
                # self._stage_lock is still held across the await, so this
                # session's own stage mutations stay serialized exactly as
                # before.
                result = await asyncio.to_thread(realtime_tools.call_tool, self.session, "go_back_or_edit", {"action": "go_back"})
                self._note_stage_for_call_ending()
                await self.client_ws.send_json({
                    "type": "stage_update", "stage": self.session.stage,
                    "slots": self.session.slots, "ui": ui_payload(),
                })
                if result.get("ok"):
                    await self._refresh_tools_for_stage()
                    say_next = result.get("say_next")
                    if say_next:
                        await self._create_response(instructions=f"Say exactly, word for word: {say_next}")
                else:
                    await self.client_ws.send_json({"type": "error", "message": result.get("error", "can't go back from here")})
            return

        if mtype == "select_option":
            # Tapping an option card, ported from the cascaded widget.js's
            # makeOptionCard(). Direct call for the same reason as above -
            # the value came from a card WE generated from ui_for_stage(),
            # so there's nothing for the MODEL to interpret; skip straight
            # to confirm_slot instead of routing it through text_input. We
            # still log the tap into the conversation via _inject_user_note
            # (see its docstring) purely so the model's own turn history
            # stays coherent - it's not asked to react to that note.
            value = msg.get("value")
            # BUG FIX: locked from the field-read onward, not just the
            # tool call - `field = self.session.stage` used to be read
            # outside any lock, so a concurrent model-driven tool call
            # (running on the other pump task) could advance the stage
            # between that read and confirm_slot actually running, letting
            # a stale click apply to the wrong field or clobber a stage
            # transition already in flight. See the lock's docstring in
            # __init__ for the exact log evidence this was producing.
            async with self._stage_lock:
                field = self.session.stage
                label = realtime_tools.option_label(self.session, field, value)
                # Worded as already-done/no-action-needed - a plain "(tapped:
                # Cooling with Electric Heat)" note was getting picked up by the
                # model as if it were a fresh spoken answer, and it would try to
                # confirm_slot it AGAIN a beat later (using the label text as
                # the value, not even the real slug) once the stage had already
                # moved on - the exact "not the current question" + go_back_or_edit
                # recovery loop causing the post-tap lag/odd phrasing.
                await self._inject_user_note(f"[System note: caller tapped '{label}' on screen - already saved, no tool call needed for this.]")
                # See the go_back handler above for why this is offloaded
                # to a worker thread - confirm_slot is the call site that
                # actually places the urgent-callback Twilio call / sends
                # the lead email when this tap is the one that reaches
                # plan_action or call_timing.
                result = await asyncio.to_thread(realtime_tools.call_tool, self.session, "confirm_slot", {"field": field, "value": value})
                self._note_stage_for_call_ending()
                await self.client_ws.send_json({
                    "type": "stage_update", "stage": self.session.stage,
                    "slots": self.session.slots, "ui": ui_payload(),
                })
                if result.get("ok"):
                    await self._refresh_tools_for_stage()
                    say_next = result.get("say_next")
                    if say_next:
                        await self._create_response(instructions=f"Say exactly, word for word: {say_next}")
                else:
                    await self.client_ws.send_json({"type": "error", "message": result.get("error", "couldn't select that option")})
            return

        if mtype == "text_input":
            # Typed fallback (see frontend #stage-ui text box). Deliberately
            # NOT routed through DialogueManager.handle_turn/handle_manual -
            # doing that would create a second, parallel way to mutate
            # session.stage/slots that the live OpenAI conversation doesn't
            # know about, and the two could disagree about what stage we're
            # on. Instead this is injected as an ordinary user turn into the
            # SAME conversation, so the model uses its normal tools
            # (confirm_slot/go_back_or_edit/etc) exactly as it would for a
            # spoken turn - review_summary corrections, edit-field, all of
            # it keeps working with zero special-casing.
            text = (msg.get("text") or "").strip()
            if not text:
                return
            await self.oa_ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }))
            await self._create_response()
            return

        if mtype == "replay":
            # Header "Replay" button. Reuses go_back_or_edit's own repeat
            # text directly rather than routing back through the model as
            # a tool call - and uses response.create's per-response
            # `instructions` override so this one utterance doesn't touch
            # the cached static instructions block or add a permanent
            # item to the conversation history.
            # See the go_back handler's comment above for why this runs in
            # a worker thread.
            result = await asyncio.to_thread(realtime_tools.call_tool, self.session, "go_back_or_edit", {"action": "repeat"})
            say_next = result.get("say_next") if result.get("ok") else None
            if say_next:
                await self._create_response(instructions=f"Say exactly, word for word: {say_next}")
            return

        if mtype == "edit_field":
            # Sidebar "Change" button next to tonnage/location/plan - the
            # explicit, clickable discoverability fix for callers who
            # don't know a spoken edit is even possible. Same direct-call
            # pattern as replay: run the tool server-side, force the
            # resulting question to be spoken, no full session.update.
            field = msg.get("field")
            # BUG FIX: locked for the same reason as go_back/select_option
            # above - see the lock's docstring in __init__.
            async with self._stage_lock:
                await self._inject_user_note(f"[System note: caller tapped Change next to {field} - already handled, no tool call needed.]")
                # See the go_back handler's comment above for why this runs
                # in a worker thread.
                result = await asyncio.to_thread(realtime_tools.call_tool, self.session, "go_back_or_edit", {"action": "edit_field", "field": field})
                self._note_stage_for_call_ending()
                await self.client_ws.send_json({
                    "type": "stage_update", "stage": self.session.stage,
                    "slots": self.session.slots, "ui": ui_payload(),
                })
                if result.get("ok"):
                    await self._refresh_tools_for_stage()
                    say_next = result.get("say_next")
                    if say_next:
                        await self._create_response(instructions=f"Say exactly, word for word: {say_next}")
                else:
                    await self.client_ws.send_json({"type": "error", "message": result.get("error", "couldn't edit that field")})
            return

        if mtype == "schedule_pick":
            # Calendar widget's slot button (see _schedule_ui in
            # state_machine.py / makeScheduleWidget in realtime-widget.js).
            # Same direct-call pattern as select_option: the caller already
            # picked an exact date+time from a grid we generated - already-
            # booked slots were filtered out server-side, so there's
            # nothing for the model to interpret or compute. This is the
            # whole point of offering the widget as an alternative to
            # voice/typed scheduling: it skips the natural-language date
            # resolution entirely instead of asking the model to do it.
            call_date = msg.get("date")
            call_time = msg.get("time")
            async with self._stage_lock:
                await self._inject_user_note(
                    f"[System note: caller picked {call_date} at {call_time} on the calendar widget - already handled, no tool call needed.]"
                )
                # See the go_back handler's comment above for why this runs
                # in a worker thread - schedule_appointment is the call
                # site that hits Google Calendar's (also synchronous)
                # events().insert().execute() call.
                result = await asyncio.to_thread(
                    realtime_tools.call_tool,
                    self.session, "schedule_appointment", {"call_date": call_date, "call_time": call_time},
                )
                self._note_stage_for_call_ending()
                await self.client_ws.send_json({
                    "type": "stage_update", "stage": self.session.stage,
                    "slots": self.session.slots, "ui": ui_payload(),
                })
                if result.get("ok"):
                    await self._refresh_tools_for_stage()
                    say_next = result.get("say_next")
                    if say_next:
                        await self._create_response(instructions=f"Say exactly, word for word: {say_next}")
                else:
                    await self.client_ws.send_json({"type": "error", "message": result.get("error", "couldn't book that slot")})
            return

    async def _pump_openai_to_client(self):
        async for raw in self.oa_ws:
            event = json.loads(raw)
            await self._handle_event(event)

    async def _handle_event(self, event: dict):
        self._note_activity()
        etype = event.get("type")

        if etype == "response.output_audio.delta":
            if self._response_first_audio_at is None and self._response_create_sent_at is not None:
                self._response_first_audio_at = time.monotonic()
                ttfb = self._response_first_audio_at - self._response_create_sent_at
                logger.info("[%s] TTFB stage=%s kind=%s %.2fs",
                            self.session.session_id, self._response_for_stage, self._response_kind, ttfb)
            if self._call_ending and self._call_ending_at is None:
                self._call_ending_at = time.monotonic()
            audio_bytes = base64.b64decode(event["delta"])
            self._audio_bytes_forwarded += len(audio_bytes)
            await self.client_ws.send_bytes(audio_bytes)
            return

        if etype == "input_audio_buffer.speech_started":
            self._bytes_since_speech_started = 0
            self._speech_started_at = time.monotonic()
            await self.client_ws.send_json({"type": "barge_in_start"})
            return

        if etype == "input_audio_buffer.speech_stopped":
            await self.client_ws.send_json({"type": "barge_in_end"})
            # BUG FIX: this used to fire _create_response() unconditionally
            # on every speech_stopped. A noise/echo blip that only barely
            # crossed the client's local VAD gate still opens a server VAD
            # turn - when that happened, the model had to say SOMETHING for
            # essentially silent/noise audio, which produced hallucinated
            # foreign-language transcripts and guessed slot values (e.g.
            # confirming "garage" with nothing actually said). A real
            # spoken answer - even one word - streams well more than this
            # many bytes (24kHz/16-bit mono = ~48,000 bytes/sec) by the
            # time VAD decides the turn ended, so anything under this is
            # almost certainly a false trigger - drop it instead of
            # forcing a guess.
            if self._bytes_since_speech_started < 14000:  # ~290ms of audio
                logger.info(
                    "[%s] ignoring VAD blip (%d bytes, likely noise not speech)",
                    self.session.session_id, self._bytes_since_speech_started,
                )
                # The matching Whisper aux-transcript for this blip is
                # still coming (async) - flag it so the transcription
                # handler below drops it too instead of showing a
                # hallucinated caption.
                self._ignored_blip_count += 1
                return
            # BUG FIX (user request): even confirmed real speech used to
            # send response.cancel here, cutting the bot off mid-sentence
            # for ANY audible voice - including a quiet one. The caller
            # wants the bot to always finish speaking uninterrupted; a
            # real turn now just queues behind the active response via
            # _create_response()'s existing queueing (see response.done)
            # instead of cancelling it. This also removes the occasional
            # "response_cancel_not_active" server error from cancelling
            # a response that had already finished by the time this ran.
            # The buffer still auto-commits to a conversation item with
            # create_response=False - we just have to ask for the
            # response ourselves now. Routing it through _create_response()
            # (not a raw send) means a stray echo turn queues behind an
            # in-flight button-click response instead of racing it.
            await self._create_response()
            return

        if etype == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            # BUG FIX: this fires independently of the noise-blip filter
            # in speech_stopped, so a blip we already decided to ignore
            # for response purposes was still being sent through Whisper
            # and shown/logged verbatim - which is where the hallucinated
            # "Bye.", "Thank you.", "you" captions on near-silent audio
            # came from. Drop it if it matches an ignored blip.
            if self._ignored_blip_count > 0:
                self._ignored_blip_count -= 1
                logger.info(
                    "[%s] dropping caller transcript for ignored blip: %r",
                    self.session.session_id, transcript,
                )
                return
            logger.info("[%s] caller (aux transcript): %r", self.session.session_id, transcript)
            session_store.log_transcript_turn(self.session.session_id, "caller", transcript)
            # Same bug as the assistant transcript below: only reaches the
            # server log. Typed text already echoes locally in the browser
            # (see realtime-widget.js's submit()), so this only fires for
            # actual spoken turns - safe to forward without double-printing
            # typed answers.
            if transcript:
                await self.client_ws.send_json({"type": "caller_transcript", "text": transcript})
            return

        if etype == "response.output_audio_transcript.delta":
            # Mirrors response.output_audio.delta below - text streams in
            # step with audio instead of only appearing once the whole
            # line is done generating (which on a long line like
            # review_summary meant the caption showed up after most of
            # the audio had already played). NOTE: event/field names
            # follow OpenAI's realtime transcript-delta convention but
            # aren't verified against a live connection in this sandbox -
            # confirm against a real call/current docs (see module
            # docstring's existing caveat).
            delta = event.get("delta", "")
            if delta:
                await self.client_ws.send_json({"type": "assistant_transcript_delta", "text": delta})
            return

        if etype == "response.output_audio_transcript.done":
            transcript = event.get("transcript", "")
            logger.info("[%s] bot (aux transcript): %r", self.session.session_id, transcript)
            session_store.log_transcript_turn(self.session.session_id, "assistant", transcript)
            # BUG FIX: this used to be held until response.done to guard
            # against a barge-in-truncated response showing a caption for
            # words never heard - but interrupt_response and our own
            # manual response.cancel are both gone now (nothing truncates
            # a response mid-flight anymore), so that guard was pure
            # added latency with nothing left to protect against - worst
            # on the long review_summary readback. Forward immediately.
            if transcript:
                await self.client_ws.send_json({"type": "assistant_transcript", "text": transcript})
            return

        if etype == "response.function_call_arguments.done":
            await self._handle_tool_call(event)
            return

        if etype == "response.created":
            self._response_active = True
            return

        if etype == "response.done":
            # This is how you check what a test call actually cost -
            # see session_store.log_realtime_usage /
            # GET /api/session/{id}/usage instead of guessing from the
            # per-minute estimate.
            resp = event.get("response") or {}
            usage = resp.get("usage")
            if usage:
                session_store.log_realtime_usage(self.session.session_id, usage)
                logger.info("[%s] response usage: %s", self.session.session_id, usage)
                logger.info("[%s] forwarded %d audio bytes to client for this response", self.session.session_id, self._audio_bytes_forwarded)
                self._audio_bytes_forwarded = 0
                in_details = usage.get("input_token_details") or {}
                logger.info("[%s] perf stage=%s kind=%s input_tokens=%s cached_tokens=%s output_tokens=%s",
                            self.session.session_id, self._response_for_stage, self._response_kind,
                            usage.get("input_tokens"), in_details.get("cached_tokens"), usage.get("output_tokens"))
            status = resp.get("status")
            if status not in (None, "completed"):
                # BUG FIX: a failed/cancelled response used to just log
                # this and fall through silently - the caller hears dead
                # air with no idea anything went wrong. Retry once instead
                # of dying silent.
                logger.info("[%s] response ended with status=%s - retrying", self.session.session_id, status)
                self._response_active = False
                await self._create_response()
                return
            self._response_active = False
            while self._pending_responses:
                # Prefer a scripted (instructions-bearing) entry over a bare
                # one so the real next question always gets the next turn.
                # Nothing is dropped - any bare entry stays queued and still
                # fires right after it.
                idx = next(
                    (i for i, (instr, _) in enumerate(self._pending_responses) if instr is not None),
                    0,
                )
                instructions, for_stage = self._pending_responses.pop(idx)
                # BUG FIX: dropping every queued entry whose instructions
                # were None (in addition to stage-stale ones) was wrong. A
                # REAL caller answer that arrives while the bot is still
                # mid-sentence also queues via a bare _create_response()
                # with no instructions - see speech_stopped above, which
                # only reaches that call after its blip filter has already
                # confirmed the audio is real speech, not noise/echo. That
                # is the ONLY path a real spoken answer takes to reach the
                # model when it overlaps the bot talking, so dropping it
                # unconditionally silently ate legitimate answers - this is
                # what produced "I answered, it went silent, I had to
                # repeat myself." Stage staleness is the only real reason
                # to drop a queued entry; a same-stage unscripted entry is
                # a real answer waiting its turn and must still fire.
                if for_stage != self.session.stage:
                    logger.info(
                        "[%s] dropping stale response (was for stage=%s, now at %s)",
                        self.session.session_id, for_stage, self.session.stage,
                    )
                    continue
                await self._create_response(instructions=instructions)
                break
            if not self._response_active:
                self._awaiting_caller_since = time.monotonic()
            return

        if etype == "error":
            err = event.get("error") or {}
            if err.get("code") == "input_audio_buffer_commit_empty":
                # Push-to-talk released with nothing actually captured
                # (accidental tap, or held too briefly). Not a real
                # failure - don't surface a scary error or let a
                # response fire on zero audio, which is what was
                # producing hallucinated replies disconnected from
                # anything the caller said.
                self._response_active = False
                logger.info("[%s] push-to-talk released with no audio - ignoring", self.session.session_id)
                return
            logger.error("[%s] realtime API error: %s", self.session.session_id, event)
            self._response_active = False
            await self.client_ws.send_json({"type": "error", "message": "voice engine reported an error"})
            return

    async def _handle_tool_call(self, event: dict):
        call_id = event.get("call_id")
        name = event.get("name")
        try:
            args = json.loads(event.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}

        # BUG FIX: holds _stage_lock for the whole mutate-then-notify
        # sequence below, so a concurrent select_option/go_back/edit_field
        # click (running on the other pump task) can't sneak a stage
        # mutation in through one of the awaits in the middle of this
        # method - see the lock's docstring in __init__.
        async with self._stage_lock:
            prev_stage = self.session.stage
            # BUG FIX (latency): this is the call site that produced the
            # ~8s stall observed on call_timing='immediate' in testing -
            # confirm_slot there runs call_service.trigger_immediate_call(),
            # which does a blocking smtplib SMTP send AND a blocking
            # Twilio client.calls.create() one after another, both inline
            # on the event loop. Same issue applies to any tool call that
            # ends in _submit_lead_once() or Google Calendar's execute().
            # See the go_back handler's comment further up for the full
            # explanation of why asyncio.to_thread is the fix.
            result = await asyncio.to_thread(realtime_tools.call_tool, self.session, name, args)
            # This log line is the actual fix for "guessing instead of
            # diagnosing" - every prior fix inferred the model's behavior
            # from the dialogue/bot-transcript logs alone, which only show
            # SUCCESSFUL state changes and final spoken text. A rejected or
            # duplicate tool call - which is exactly what a "field is not
            # the current question" error looks like - was previously
            # invisible. Log every attempt, success or failure.
            logger.info(
                "[%s] tool_call %s(%s) at stage=%s -> ok=%s%s",
                self.session.session_id, name, args, prev_stage, result.get("ok"),
                "" if result.get("ok") else f" error={result.get('error')!r}",
            )
            self._note_stage_for_call_ending()

            await self.oa_ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result),
                },
            }))
            await self.client_ws.send_json({
                "type": "stage_update",
                "stage": self.session.stage,
                "slots": self.session.slots,
                "ui": DialogueManager(self.session).ui_for_stage(),
            })

            if self.session.stage != prev_stage:
                await self._refresh_tools_for_stage()

            say_next = result.get("say_next")
            if say_next:
                await self._create_response(instructions=f"Say exactly, word for word: {say_next}")
            elif not result.get("ok"):
                pass
            else:
                await self._create_response()