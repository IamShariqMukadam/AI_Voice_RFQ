# Demo Notes — Speech-to-Speech Migration (OpenAI Realtime API)

## 1. What changed, in one line

The old pipeline was a **cascade**: record audio → Whisper (Groq) turns it into text → an LLM/state machine decides the reply → edge-tts turns that reply back into audio. That's three separate steps, each with its own delay, stacked one after another.

The new pipeline is **one continuous phone call**: the browser streams raw mic audio to OpenAI's Realtime API over a WebSocket, and OpenAI streams spoken audio back — same connection, both directions, no "record → wait → get a reply" cycle. This branch removes the old cascade entirely (it's still untouched on `main` as a fallback).

```
OLD (cascade):  mic → record clip → Whisper STT → LLM+FSM → edge-tts → play clip
NEW (realtime): mic → stream PCM ──────────────► OpenAI Realtime API ──────────────► stream audio back
                         (continuous, both directions, one socket, no clip boundaries)
```

## 2. Architecture — who talks to whom

```
Browser (realtime-widget.js)
   │  raw PCM16 audio chunks, continuously
   │  + JSON control messages (button clicks, typed answers)
   ▼
FastAPI  /ws/realtime/{session_id}   (main.py)
   │
   ▼
RealtimeSession (services/realtime_session.py)
   │  one instance per call — owns the whole conversation
   │
   ▼
OpenAI Realtime API  (wss://api.openai.com/v1/realtime)
   │  when the model decides it needs to act (not just talk),
   │  it calls a "tool" instead of speaking
   ▼
realtime_tools.call_tool()  (services/realtime_tools.py)
   │  routes to the SAME deterministic functions the old
   │  system used: DialogueManager, plan_matcher, notify,
   │  session_store, calendar_service — nothing about
   │  pricing/validation was reimplemented
   ▼
dialogue/state_machine.py  (unchanged — the FSM: full_name → phone →
                             category → location → plan_choice → ...)
```

**The key design decision**: the model never invents a price, a plan, or the next question. It can only call `confirm_slot(field, value)`, and the *code* decides what happens next and hands the model an exact sentence (`say_next`) to speak. This is the same "deterministic-first, AI only parses messy input" principle the old cascade used — it just carries over into the new pipeline.

---

## 3. File-by-file: what's new and how it works

### `backend/config.py` — every tunable knob in one place

This is where the Realtime API gets configured. A few choices worth being able to explain:

```python
REALTIME_MODEL = os.environ.get("REALTIME_MODEL", "gpt-realtime-2.1-mini")
REALTIME_VAD_MODE = os.environ.get("REALTIME_VAD_MODE", "far_field")
REALTIME_VAD_TYPE = os.environ.get("REALTIME_VAD_TYPE", "semantic_vad")
REALTIME_IDLE_TIMEOUT_SECONDS = float(os.environ.get("REALTIME_IDLE_TIMEOUT_SECONDS", "120"))
REALTIME_MAX_CALL_SECONDS = float(os.environ.get("REALTIME_MAX_CALL_SECONDS", "300"))
```

- **`far_field` noise mode**: this is a speakerphone bot (car, shop, kitchen), not a headset bot — `far_field` is tuned for a mic that isn't right next to your mouth.
- **`semantic_vad`**: instead of "wait for N ms of silence," OpenAI judges from *what was said* whether the caller is actually done talking (fewer false cut-offs on breaths/pauses).
- **`IDLE_TIMEOUT` / `MAX_CALL_SECONDS`**: OpenAI bills the connection per-minute while it's open, whether anyone's talking or not — these are cost safety nets so a forgotten open tab or a caller who never hangs up doesn't bill forever.

### `backend/main.py` — the FastAPI app

Two endpoints matter here:

**`POST /api/session/start`** — just creates a session row and returns a `session_id`. Notice it does *not* generate any audio — the old system generated the greeting audio here with edge-tts; now the greeting is spoken live once the WebSocket connects, same as every other turn.

**`WS /ws/realtime/{session_id}`** — the actual call. This is deliberately thin: it authenticates, then hands off everything to `RealtimeSession`:

```python
@app.websocket("/ws/realtime/{session_id}")
async def realtime_ws(websocket: WebSocket, session_id: str, api_key: str = Query(default="")):
    await websocket.accept()
    session = _get_session(session_id)
    _consume_api_key_once(session, api_key)   # billed once per real call, not per reconnect
    rt_session = RealtimeSession(session, websocket)
    await rt_session.run()
```

`api_key` travels as a **query param**, not a header — a browser WebSocket can't set custom headers on the connection request, so this is the only place a key can go.

There's also a small billing correctness fix worth mentioning if asked: `_check_api_key` (validates, doesn't charge) is called on every page load/resume, but `_consume_api_key_once` (charges quota) only fires once per real call, guarded by a flag stored on the session (`session.slots["_quota_charged"]`). Before this, a single call could get billed 2–3 times if the browser reconnected.

### `backend/services/realtime_session.py` — the core of the new system (~970 lines)

One `RealtimeSession` object = one live call. It:

1. Opens a WebSocket to OpenAI (`wss://api.openai.com/v1/realtime`), authenticated with a plain bearer token.
2. Sends a **`session.update`** once, describing the whole call: system instructions, VAD settings, audio format, and the list of tools available.
3. Runs two loops concurrently with `asyncio.gather`-style tasks:
   - `_pump_client_to_openai` — forwards mic audio from the browser straight to OpenAI.
   - `_pump_openai_to_client` — forwards OpenAI's spoken audio and events back to the browser.
4. Reacts to events: speech started/stopped, transcripts, tool calls, usage stats, errors.

**Why instructions are sent once and never resent** — this is a genuinely important detail if asked "why not just resend the prompt every turn?":

```python
async def _refresh_tools_for_stage(self):
    """Called whenever confirm_slot/go_back_or_edit change session.stage.
    Deliberately sends ONLY `tools` - omitting `instructions` leaves the
    cached static block untouched server-side instead of re-billing/
    re-caching it on every stage change."""
    tools = realtime_tools.tools_for_stage(self.session)
    await self.oa_ws.send(json.dumps({
        "type": "session.update",
        "session": {"type": "realtime", "tools": tools},
    }))
```

`session.update` is a *partial* update — anything you don't include stays as-is. OpenAI caches the instructions text server-side; resending the same long block every time a stage changes would break that cache and cost more. So only the (short) `tools` list is resent per stage — the instructions are sent exactly once, at connect time.

**The response-queueing problem** — this is the best "hardest bug" story in the whole migration:

There are six different things that can each want the assistant to speak next: a tool call finishing, a button click, typed text, push-to-talk release, and OpenAI's own voice-activity detector. If two of those fire close together, OpenAI rejects the second `response.create` with `conversation_already_has_active_response`. The fix is a simple queue:

```python
self._response_active = False
self._pending_responses: list[tuple[str | None, str]] = []  # (instructions, for_stage)
```

Every response request goes through `_create_response()`, which checks `_response_active` and queues instead of firing blind. When `response.done` comes back, it pops the queue — but only if the queued item still matches the *current* stage, and only if it has real instructions (not `None`). That second check exists because a stray voice-activity trigger — usually the bot's own TTS echoing into the mic — has no real instructions attached, so it's dropped unconditionally rather than replayed.

**The "is that really speech?" filter** — a raw mic doesn't know a breath or a click apart from a word. The fix counts *bytes streamed since speech was flagged as started*, and only treats it as a real turn past a threshold:

```python
if self._bytes_since_speech_started < 14000:  # ~290ms of audio
    logger.info("[%s] ignoring VAD blip (%d bytes, likely noise not speech)", ...)
    self._ignored_blip_count += 1
    return
```
(24kHz, 16-bit mono ≈ 48,000 bytes/second, so 14,000 bytes is roughly a third of a second — too short to be a real spoken answer.) The matching Whisper caption for that same blip is also dropped, so the UI never shows a hallucinated "Bye." or "Thank you." for a noise that was never actually said.

**A concurrency bug worth mentioning if asked about the hardest thing to debug**: two different code paths can each change `session.stage` — a model tool call, and a button click on the frontend — and both `await` multiple times mid-change (network sends in between). Since they run as separate async tasks, one could interleave a stage change into the middle of the other's in-progress change, producing "impossible" log sequences. Fixed with a lock around the full mutate-then-notify sequence:

```python
self._stage_lock = asyncio.Lock()
...
async with self._stage_lock:
    # read stage, run the tool, mutate stage, notify the client — atomically
```

**Blocking calls off the event loop** — some tool handlers do blocking network I/O (SMTP email, Twilio's REST call, Google Calendar's API). Running those inline on the event loop stalled the *entire* call — every caller's audio froze — for as long as that blocking call took (an ~8 second stall was observed on "call me immediately"). Fix: `asyncio.to_thread`:

```python
result = await asyncio.to_thread(realtime_tools.call_tool, self.session, name, args)
```

### `backend/services/realtime_tools.py` — the tools the model can call

This file does **not** reimplement any business logic. It exposes the existing `DialogueManager`, `plan_matcher`, `notify`, `session_store`, and `calendar_service` functions as callable "tools," plus guard rails a raw function call wouldn't have:

```python
def handle_confirm_slot(session, field, value):
    if field != session.stage:
        return {
            "ok": False,
            "error": f"'{field}' is not the current question - the caller is on '{session.stage}'. ..."
        }
    ...
    dm = DialogueManager(session)
    _, speech_text = dm.handle_manual(field, value)   # same validation as the old typed-input path
    return {"ok": True, "stage": session.stage, "say_next": speech_text, "slots": session.slots}
```

The important guard: `field` must match the caller's *actual current stage*. Without this, a wrong or confused tool call could silently skip the FSM ahead. Editing an earlier answer has to go through a separate `go_back_or_edit` tool first, which moves the stage back *before* `confirm_slot` is allowed to touch it.

`say_next` is the anti-hallucination guarantee: it's the exact same templated sentence the old cascade would have spoken, including any prices. The model is instructed to say that string verbatim, never to paraphrase it — so it can never misquote a price or invent a plan that doesn't exist.

Other tools in this file: `get_plan_pricing`, `schedule_appointment` (validates date/time format, checks it's within `SCHEDULE_MAX_DAYS_AHEAD`, books the slot), and `go_back_or_edit` (repeat / start over / go back / jump to an earlier field).

### `backend/services/session_store.py` — SQLite, extended for the realtime path

Same SQLite file as before, with new tables specifically for the realtime pipeline:

- `realtime_transcripts` — every caller/assistant turn, for reviewing what was actually said after a call.
- `realtime_usage` — logs OpenAI's own reported token/cost usage from each `response.done` event, so `GET /api/session/{id}/usage` shows real cost instead of an estimate.
- `booked_slots` — appointment scheduling, with a uniqueness check so two callers can't double-book the same slot.
- `api_keys` — for running this as a paid API-as-a-service: per-client key, plan tier, monthly quota, with `check_and_increment_api_key` doing the atomic "is there quota left, and use one" check in a single DB call.

### `backend/services/calendar_service.py` and `call_service.py` — optional integrations

Both follow the same pattern: **fully optional, never breaks the core flow if unconfigured.**

- `calendar_service.py` creates a Google Calendar event for a booked appointment. If `GOOGLE_SERVICE_ACCOUNT_FILE` isn't set, it just logs and returns `None` — the booking and lead email still happen either way. (Worth knowing: a bare service account can't email the customer an invite without domain-wide delegation — that's a Google Workspace admin setup step, documented in the file, not a code limitation.)
- `call_service.py` handles "call me right now": it **always** sends an urgent lead email first (guaranteed, no external service needed), and *optionally* places a real outbound call through Twilio if it's configured, dialing the org first, then bridging in the customer.

### `frontend/realtime-widget.js` — the browser side (~900 lines)

This replaced the old record-a-clip-and-POST-it widget. Key pieces:

**Mic capture and local VAD.** The browser doesn't just stream raw audio blindly — it calibrates a noise floor first (samples ambient room noise for a moment when the mic opens), then only streams audio that's clearly above that floor:

```javascript
function localVadGate(float32Array) {
  const rms = _rms(float32Array);
  if (rms >= VAD_ON_THRESHOLD) { vadIsSpeaking = true; ... }
  else if (rms < VAD_OFF_THRESHOLD) { if (vadHangover > 0) vadHangover--; else vadIsSpeaking = false; }
  return vadIsSpeaking;
}
```

This is a *client-side* gate that runs in front of OpenAI's own server-side VAD — it exists to stop obvious silence/room-noise from ever being sent at all, saving bandwidth and avoiding false triggers before the audio even leaves the browser. It also has a backoff: if nothing crosses the threshold for 4 seconds, thresholds loosen by half (maybe the caller is speaking quietly); if a strong signal shows up again, thresholds snap back to the calibrated value instead of staying permanently loosened.

**Resampling.** The Realtime API expects 24kHz PCM16 audio. Not every browser's `AudioContext` actually honors a requested sample rate (Safari/iOS in particular), so the widget measures the *real* rate it got and linearly resamples every chunk before sending:

```javascript
micResampleRatio = audioCtx.sampleRate / 24000;
// ... later, per audio chunk:
const resampled = micResampleRatio === 1 ? input : resampleLinear(input, micResampleRatio);
const pcm16 = floatTo16BitPCM(resampled);
ws.send(pcm16);
```

**Format fallback per browser.** Safari doesn't support the same recording formats other browsers do — the widget picks from a supported-format list rather than assuming one, so iPhone recordings don't silently fail.

**Reconnect handling.** If the WebSocket drops mid-call, `attemptReconnect()` retries rather than just dying — since this is a continuous connection now (not a per-turn request), a drop is more disruptive than it was on the old cascade.

### `dialogue/state_machine.py`, `slots.py`, `services/extraction.py`, `plan_matcher.py`, `notify.py`

**Not new** — this is the same deterministic FSM and business logic the old cascade used. This is the whole point: the voice *transport* changed completely (cascade → realtime), but the thing deciding what question comes next, what a valid answer looks like, and which plan matches, is untouched. That's what makes the "AI never invents a price" guarantee hold.

---

## 4. One full turn, start to finish

1. Caller speaks. Browser streams raw PCM continuously (no record/stop step) once it clears the local VAD gate.
2. OpenAI's own server-side VAD (`semantic_vad`) decides the caller has finished the thought.
3. The model either replies directly, or — if it has a clear answer for the current field — calls `confirm_slot(field, value)`.
4. `realtime_tools.call_tool()` runs the *same* validation the old typed-input path used, updates `session.stage`, and returns `{"ok": true, "say_next": "<exact next line>"}`.
5. The backend tells the model: *"Say exactly, word for word: `<say_next>`."* The model is instructed never to improvise this part.
6. The reply streams back as audio, chunk by chunk, and plays in the browser while it's still arriving (not after the whole sentence is generated).
7. The frontend also gets a `stage_update` event with the new `slots` dict, so the right-hand-side detail panel updates live.

---

## 5. How to run the demo

```bash
cd backend
venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`. For mobile testing (microphone access needs HTTPS on phones):

```bash
npx localtunnel --port 8000
```

Check what a call actually cost after testing, instead of guessing:

```bash
curl http://localhost:8000/api/session/<session_id>/usage
```

---

## 6. Likely questions and answers

**Q: What's actually different from before — didn't you already have a voice assistant?**
Yes — the previous version worked but was a cascade: record → transcribe → decide → synthesize speech, one step at a time. This version is a single continuous connection where the model listens and speaks in the same stream, which is faster and feels more like a real phone call.

**Q: Why not let the model just talk freely instead of using tools?**
Same reason as before: pricing and plan names can't be hallucinated. The model can only call `confirm_slot`, and the *code* — not the model — decides the next question and hands the model the exact sentence to say.

**Q: What was the hardest bug in this migration?**
Probably the response-queueing issue: six different triggers (tool calls, clicks, typed text, push-to-talk, voice detection) could each try to start a spoken response, and OpenAI rejects a second one while one's in flight. Fixed with a queue that also drops stale or unscripted entries — see `realtime_session.py`'s `_create_response`/`response.done` handling.

**Q: How do you stop background noise from being treated as speech?**
Two layers: a client-side VAD gate in the browser that calibrates to the room before streaming anything, and a server-side byte-count check — if a "turn" contains under ~290ms of audio, it's almost certainly a noise blip, not a real answer, and gets dropped along with its transcript.

**Q: Is this billed differently from the old system?**
Yes — OpenAI bills the Realtime connection per minute while it's open, not per API call. That's why there's an idle timeout (120s of no activity) and a hard max call length (300s) — nothing used to hang up the connection server-side, so an open tab could bill indefinitely.

**Q: Does the old system still exist?**
Yes, untouched on `main`. This is a separate branch specifically to validate the Realtime API before deciding whether to fully replace the cascade.

**Q: Is this production-ready?**
Closer than before, but the file's own notes are honest about a gap: it hasn't been tested against a live OpenAI key in this environment (no outbound network access here), so the event names/payload shapes should be double-checked against OpenAI's current docs before trusting it fully in production.

---

## 7. Bugs found and fixed since this doc was first written

Real bugs, each traced through `session_id → stage → value` logs against actual test calls:

- **Greeting text leaking into a slot value.** The opening line was, on one path, being
  routed through the same handling as a caller's spoken answer — fixed so the greeting
  is only ever spoken, never treated as an answer to confirm.
- **`semantic_vad` stalling indefinitely on the street-address field.** OpenAI's own
  end-of-turn detector can just never decide the caller is finished (ambient noise, a
  dropped frame). Audio keeps streaming in, so the idle timeout never trips either.
  Fixed with `_watchdog()` forcing a manual `input_audio_buffer.commit` if no turn
  boundary arrives within `REALTIME_STUCK_TURN_SECONDS` (22s, tuned up from an
  initial 12s that fired on ordinary multi-choice think-time).
- **That same watchdog fix then clipped mid-word audio.** A caller saying "Pune" got
  force-committed right as they started speaking, and the model heard a truncated
  "Une." Fixed with a 2-second grace window after speech onset before a forced commit
  is allowed to fire (`recent_speech_onset` check in `_watchdog`).
- **A forced commit on a genuinely empty buffer caused a hallucinated answer** — a
  `full_name` got corrupted this way in testing, because OpenAI was still asked to
  respond even with nothing in the buffer. Fixed by checking
  `_bytes_since_last_commit == 0` first and just restarting the wait window instead.
- **Pending:** conversation history isn't trimmed yet, so per-turn latency grows over
  a long call — needs periodic `conversation.item.delete` calls. The AudioWorklet
  migration (`frontend/audio-worklets.js`) has both processors written but
  `realtime-widget.js` isn't wired up to use them yet, so `ScriptProcessorNode` is
  still what's actually running mic capture/playback today.