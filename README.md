# AC / Heating Instant Quote — Voice Assistant

A voice bot that replaces the manual multi-step quote form with a real phone-call-like
conversation. Caller talks, the bot asks the same questions the form used to ask, and
a lead lands in the same place at the end. Same data, same outcome — different front door.

Standalone widget + API. It does not touch the existing WordPress site or plugin —
embed the widget wherever you decide to put it; the manual form stays untouched as a fallback.

> **Note:** this describes the current WebRTC-direct architecture (Generation 2). An
> earlier WebSocket-relay version (Generation 1) still exists in this repo as a fallback —
> see §3.1, and `ARCHITECTURE.md` §4 for the full history.

---

## 1. What this is, in 30 seconds

| | |
|---|---|
| **Input** | Caller's voice, streamed live from the browser mic (or typed/tapped, as a fallback) |
| **Engine** | OpenAI Realtime API over WebRTC — audio flows directly between the browser and OpenAI, no relay through the backend |
| **Brain** | A deterministic state machine (FSM) — not the model — decides questions, prices, and plans |
| **Backend's job** | Mint a short-lived token once at call start, then run business logic per tool call over plain REST — never touches audio |
| **Output** | Spoken replies streamed back live, plus a lead delivered by email/CRM/file |
| **Guarantee** | The model can never invent a price, a plan, or skip a question — the worst it can do is ask you to repeat yourself |

---

## 2. Why it's built this way

**The core problem with "just let the AI talk":** an LLM speaking freely can hallucinate
a price, invent a plan that doesn't exist, or misquote availability. For something that
turns into a real customer lead, that's not acceptable.

**The fix:** the model is only ever allowed to do one thing when it has a clear answer —
call a tool like `confirm_slot(field, value)`. It is never allowed to compose the next
question or state a price itself. The *code* (`dialogue/state_machine.py`) decides what
happens next and hands the model an exact sentence to speak. The model is instructed to
say that sentence verbatim, not paraphrase it.

This means the AI's only real job is **understanding messy human speech** — parsing what
someone said into a clean value. It never gets to decide what's true.

---

## 3. Architecture

### 3.1 How the project got here (three versions, same brain)

This started as a **cascade**: record a clip → Groq Whisper transcribes it → an
LLM/FSM decides the reply → edge-tts synthesizes speech → play the clip. Four sequential
steps, each with its own delay.

It was then migrated to **speech-to-speech over a WebSocket relay** (Generation 1): the
browser streamed raw mic audio to this backend, which relayed every frame both directions
between the browser and OpenAI's Realtime API over a plain WebSocket
(`/ws/realtime/{session_id}`, `RealtimeSession` in `realtime_session.py`). This removed the
record → wait → reply cycle, but the backend was still in the audio path twice per frame —
combined with a hand-rolled jitter buffer standing in for what a real transport does
natively, that caused a real "choppy/laggy audio" bug.

It has since been migrated again, to **WebRTC direct** (Generation 2 — current): the
browser opens its own WebRTC peer connection straight to OpenAI. Audio never transits this
backend at all. The backend's only jobs at call time are minting a short-lived token once,
and running business logic over REST once per tool call.

```
GEN 0 (cascade):   mic → record clip → Whisper STT → LLM+FSM → edge-tts → play clip
GEN 1 (relay):     mic → stream PCM ──► FastAPI WebSocket ──► OpenAI Realtime API
                            (backend relays every frame, both directions — LEGACY fallback)
GEN 2 (WebRTC):    mic ══ WebRTC, direct, both ways ══ OpenAI Realtime API
                            (backend only mints a token + runs REST tool calls — CURRENT)
```

Both older versions still exist, untouched, as fallbacks — Gen 0's cascade on `main`,
Gen 1's relay at `/ws/realtime/{id}`. This repo's active development is Gen 2.

### 3.2 Who talks to whom (current — Gen 2)

```
Browser (frontend/realtime-widget.js)
   │
   │  WebRTC audio, both directions, direct — never touches the backend
   ▼
OpenAI Realtime API  (https://api.openai.com/v1/realtime/calls)
   │
   │  data channel "oai-events" carries tool calls, transcripts, turn signals
   ▼
Browser translates a tool call into a REST request ──►
                                                        │
FastAPI  POST /api/session/{id}/tool-call  (backend/main.py)
   │
   ▼
realtime_tools.call_tool()  (backend/services/realtime_tools.py)
   │  routes to the SAME deterministic functions Gen 0/1 used:
   │  DialogueManager, plan_matcher, notify, session_store, calendar_service
   ▼
dialogue/state_machine.py + slots.py
   the FSM: full_name → phone → email → street → city → zip →
            category → tonnage → location → plan_choice → review_summary →
            plan_action → call_timing → schedule_appointment → closing
```

The backend also mints an ephemeral OpenAI token and hands back session config once, at
call start (`POST /realtime-token`, `GET /realtime-session-config`) — that's the only
other thing it does before the call ends.

### 3.3 Folder structure

```
backend/
  main.py                    FastAPI app — REST endpoints + the legacy WebSocket route
  config.py                  every tunable knob (VAD mode, timeouts, model name, etc.), one place
  dialogue/
    slots.py                  what to ask at each fixed stage, in what order
    state_machine.py          stage transitions + every spoken template (prices live here, not in the model)
  services/
    realtime_tools.py          tool definitions the model is allowed to call, plus guard rails — the model's entire API surface
    extraction.py               regex/fuzzy parsing of messy spoken/typed input
    plan_matcher.py             plan catalogue + availability lookup
    notify.py                   lead delivery: WP endpoint → SMTP → local file, in that order
    session_store.py            SQLite: sessions, transcripts, usage, bookings, API keys
    calendar_service.py         optional Google Calendar booking (safe no-op if unconfigured)
    call_service.py             "call me now" — urgent lead email always, optional Twilio call
    realtime_session.py         [LEGACY] Gen 1's WebSocket relay — fallback only, not used by the current frontend
  quote_assistant.sqlite3     local dev database
  stt_debug/                  raw audio + metadata captured for debugging transcription issues (Gen 0/1 era)
frontend/
  index.html / style.css       widget UI, no build step
  realtime-widget.js            the entire current frontend — WebRTC handshake, tool-call dispatch, UI
  audio-worklets.js              [LEGACY] Gen 1's mic/playback processors — unused on the WebRTC path
```

---

## 4. What I actually did — the build, step by step

1. **Started from a working cascade** (Groq Whisper STT → LLM-assisted FSM → edge-tts)
   that already had the deterministic dialogue engine and lead-delivery logic proven out.
2. **Migrated it to a WebSocket-relayed speech-to-speech pipeline** on OpenAI's Realtime
   API (`realtime_session.py`) — removed the record → wait → reply cycle, kept the FSM.
3. **Migrated again to WebRTC direct** — rewrote the frontend (`realtime-widget.js`) to
   open its own peer connection straight to OpenAI, cut the backend out of the audio path
   entirely, and reduced the backend's live-call job to minting a token once and running
   tool-call logic over REST. None of the FSM/pricing/validation logic was reimplemented
   in either migration — same `DialogueManager` / `plan_matcher` / `notify` /
   `calendar_service` functions throughout.
4. **Tuned voice-activity detection against real test calls**, not guesses — several
   config defaults were changed after a specific failure was observed (see
   `backend/config.py` comments for the story behind each).
5. **Found and fixed a run of real bugs** across both migrations (section 6), each traced
   through logs rather than guessed at.
6. **Left both older versions untouched** as rollback paths — the cascade on `main`, the
   WebSocket relay at `/ws/realtime/{id}` — so the current WebRTC version can be
   validated before either is fully retired.

---

## 5. Key design decisions and why

| Decision | Why |
|---|---|
| Model can only call `confirm_slot(field, value)` | Removes any path for the model to invent a price, plan, or question |
| Prices/plans/questions live in `state_machine.py`, not the prompt | A hallucination-proof source of truth that's also unit-testable with no API key |
| Audio goes browser ⟷ OpenAI directly (WebRTC); backend only sees REST | The backend is never a bottleneck or point of failure for live audio — the old relay's choppy-audio bug can't happen here |
| `session.update` sends instructions **once**, tools resent per stage | OpenAI caches the instructions server-side; resending the full block on every stage change would break that cache and cost more |
| `far_field` noise mode + `semantic_vad` | This is a speakerphone bot (car, shop, kitchen), not a headset bot — tuned for a mic that isn't close to the mouth |
| Ephemeral client token, minted server-side per call | The browser never sees the real `OPENAI_API_KEY` — only a short-lived, scoped credential |
| A response-creation queue in the frontend | Up to six different triggers can each try to start a spoken response at once; OpenAI rejects a second one while one's in flight |
| Blocking I/O (SMTP, Twilio, Calendar) run off the main thread | Without this, one slow network call froze audio for the whole call |
| Old WebSocket relay kept at `/ws/realtime/{id}` | Fallback for a network that blocks WebRTC — not used by the current frontend |

---

## 6. Bugs found and fixed (the real debugging story)

These were each found by watching logs against real test calls, not by inspection alone.

- **Greeting text was being written into a slot value.** The first turn's opening line
  was, in one path, getting treated as if it were the caller's answer to the first
  question — fixed by making sure the greeting is only ever spoken, never routed through
  the slot-confirmation path.
- **Six different triggers could each try to start a spoken response at once** (a tool
  call finishing, a button click, typed text, push-to-talk release, OpenAI's own VAD) —
  OpenAI rejects a second `response.create` while one is in flight. Fixed with a queue in
  the frontend that also drops stale or unscripted entries rather than replaying them out
  of order.
- **A send firing after the data channel had already closed threw an uncaught
  `InvalidStateError` and crashed the tab.** Fixed by routing every send through one
  `safeSend()` wrapper that checks the channel is still open first.
- **Per-turn token cost grew with call length**, since every turn's audio history stayed
  fully in-context. Fixed by trimming older conversation items after each response
  completes — the real state of what's been collected lives in `session.slots` on the
  backend, not in the model's context, so old turns were never actually needed.
- **A blocking network call (SMTP, Twilio, Calendar) froze the entire call's audio** for
  as long as the call took — an ~8s stall was observed on "call me immediately." Fixed by
  running those handlers off the main thread/event loop.
- **A single call could be billed 2–3 times** if the connection reconnected mid-call.
  Fixed by gating the charge behind a flag stored on the session.
- *(Legacy relay path only)* **`semantic_vad` stalling indefinitely, a watchdog fix that
  then clipped mid-word audio, and a forced commit on an empty buffer that hallucinated an
  answer** — three chained bugs found and fixed in sequence on the WebSocket relay path.
  That path no longer carries live traffic, so these fixes were never carried over to the
  current WebRTC path — which doesn't have this failure mode in the first place, since
  OpenAI's own server-side VAD handles turn-detection entirely there.

---

## 7. What we achieved

- A working voice assistant that's migrated twice — cascade → WebSocket relay → WebRTC
  direct — each step cutting latency and moving the backend further out of the critical
  audio path, without ever touching the FSM's pricing/validation guarantees.
- The deterministic guarantee carried over intact through both migrations: the model still
  cannot hallucinate a price or a plan, even with full conversational freedom on the
  transport layer.
- A run of real, hard-to-reproduce concurrency and rate-limit bugs found and fixed against
  actual test calls, each with a log-backed explanation of what went wrong and why the fix
  works (see section 6) — not guessed-at patches.
- Cost safety nets (client-side idle timeout, hard max call length, single-charge quota) so
  an open tab or a caller who never hangs up can't bill indefinitely.
- Optional integrations (Google Calendar booking, Twilio outbound calling) that degrade
  gracefully to "still works, just without the extra" when unconfigured, rather than
  breaking the core flow.
- Two rollback paths: the cascade untouched on `main`, and the WebSocket relay untouched
  at `/ws/realtime/{id}` — either reachable without redeploying anything.

---

## 8. Run it

```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # add OPENAI_API_KEY, plus optional SMTP/Twilio/Calendar/WP settings
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000` — it serves the frontend directly, wired to the local API.

For mobile testing (mic access needs HTTPS on phones):

```bash
npx localtunnel --port 8000
```

Check what a call actually cost after testing, instead of guessing:

```bash
curl http://localhost:8000/api/session/<session_id>/usage
```

Run tests (no API key needed — they only exercise the pure, deterministic functions):

```bash
cd backend && python -m pytest tests/ -v
```

---

## 9. Config reference (`backend/config.py`)

| Setting | Default | Why this value |
|---|---|---|
| `REALTIME_MODEL` | `gpt-realtime-2.1-mini` | Pinned to a specific snapshot on purpose |
| `REALTIME_VAD_MODE` | `far_field` | Speakerphone bot, not a headset bot |
| `REALTIME_VAD_TYPE` | `semantic_vad` | Judges from *what was said* whether the caller is done, not just silence duration |
| `REALTIME_VAD_EAGERNESS` | `medium` | Moved from `low` after a client speed complaint; `high` risks cutting callers off mid-sentence |
| `REALTIME_VAD_SILENCE_MS` | `800` | Only applies under `server_vad` — raised from `700` after a real call closed a turn too early |
| `REALTIME_STUCK_TURN_SECONDS` | `22` | *Legacy relay path only* — raised from 12 after real calls showed normal think-time on multi-choice questions running 12–13s |
| `REALTIME_IDLE_TIMEOUT_SECONDS` | `120` | *Legacy relay path only* — no server-side enforcement on the WebRTC path; that's now a client-side timer instead |
| `REALTIME_MAX_CALL_SECONDS` | `300` | *Legacy relay path only*, same caveat |
| `SCHEDULE_MAX_DAYS_AHEAD` | `30` | Server-side backstop — a model-computed date should never be trusted blindly |

---

## 10. Frequently asked (by a reviewer or teammate)

**Q: Why not let the model just talk freely instead of using tools?**
Pricing and plan names can't be hallucinated. The model can only call `confirm_slot`; the
code decides the next question and hands the model the exact sentence to say.

**Q: Does audio ever pass through the backend?**
No, not on the current (WebRTC) path — the browser talks directly to OpenAI. The backend
only mints a short-lived token at call start and runs the actual business logic once per
tool call, over plain REST. The older WebSocket relay (kept as a fallback) does put audio
through the backend, which is one reason it's no longer the default.

**Q: What was the hardest bug in this project?**
The response-queueing issue — six different triggers could each try to start a spoken
response, and OpenAI rejects a second one while one's in flight. See section 6.

**Q: How do you stop background noise from being treated as speech?**
On the current WebRTC path, this is entirely OpenAI's own server-side job
(`semantic_vad`/`server_vad`). The older relay path had an extra client-side gate plus a
server-side byte-count filter; that mechanism only exists on the legacy fallback now.

**Q: Does the old system(s) still exist?**
Yes — the original cascade is untouched on `main`, and the WebSocket-relay version is
untouched at `/ws/realtime/{id}`. Both are kept as fallbacks while this WebRTC version
gets validated.

---

For the full architecture reference — file-by-file breakdown, database schema, and known
issues/drift — see `ARCHITECTURE.md`.