# AC / Heating Instant Quote — Voice Assistant

> Architecture reference and developer onboarding guide for `ai-voice-rfq`.

| | |
|---|---|
| **Project** | `ai-voice-rfq` |
| **Branch described here** | `s2s-realtime` |
| **Client** | Polar Express A/C and Heating |
| **Audience** | Any developer picking up this codebase — including future-you |

This document is meant to be read start-to-finish, once, by anyone new to the codebase. After that first read, use it as a reference — the section index and per-file breakdowns below are written to be looked up, not re-read linearly. The goal is that nothing about this system should require asking the original author "wait, how does X work?"

### Conventions used in this document

- **`code font`** — file paths, function/variable names, config keys, and exact strings the system actually uses.
- **Bold** — a term defined in the [Glossary](#16-glossary), on its first meaningful mention.
- **`[CURRENT]` / `[LEGACY]`** tags next to a heading — whether that path is the one live traffic uses today, or a superseded/fallback path kept for a specific reason (always stated inline).
- **§N** — a cross-reference to numbered section N elsewhere in this document.
- Callout boxes mark asides that matter but would break the flow of the surrounding explanation:

> [!NOTE]
> Supplementary context — useful, not urgent.

> [!IMPORTANT]
> A fact that most of the rest of the document depends on.

> [!WARNING]
> A real gotcha, risk, or known gap — read this before touching the related code.

---

## Table of Contents

1. [Read This First — Orientation](#1-read-this-first--orientation)
2. [What This Project Is](#2-what-this-project-is)
3. [The Core Design Principle](#3-the-core-design-principle)
4. [Two Architecture Generations](#4-two-architecture-generations-important)
5. [Current Architecture — Full Diagram](#5-current-architecture--full-diagram)
6. [Repository Map](#6-repository-map)
7. [Backend — File by File](#7-backend--file-by-file)
8. [Frontend — File by File](#8-frontend--file-by-file)
9. [The Finite State Machine (FSM)](#9-the-finite-state-machine-fsm)
10. [A Call, Start to Finish](#10-a-call-start-to-finish-step-by-step-trace)
11. [Database Schema](#11-database-schema)
12. [Configuration Reference](#12-configuration-reference)
13. [Known Issues, Drift & Technical Debt](#13-known-issues-drift--technical-debt)
14. [Testing](#14-testing)
15. [Running & Deploying](#15-running--deploying)
16. [Glossary](#16-glossary)
17. [FAQ](#17-faq)

---

## 1. Read This First — Orientation

If you're new to this codebase, **read files in this order**, not alphabetically and not by folder:

1. **This document, in full**, once, before touching code.
2. `backend/dialogue/slots.py` — the shortest file that matters. It's the question list. Read it in 2 minutes and you know what the bot asks.
3. `backend/dialogue/state_machine.py` — the FSM. This is the actual brain of the product. Everything else exists to feed data into this file or to speak what it outputs.
4. `backend/services/realtime_tools.py` — how the AI model is allowed to touch the FSM above. Read `TOOL_SCHEMAS` and `_confirm_slot_schema` closely — this is the anti-hallucination mechanism, and it's the most important file in the project to understand correctly.
5. `frontend/realtime-widget.js` — the entire frontend. One file, ~1300 lines, no build step, no framework. Read the top-of-file comment block first, then `connectWebRTC()`, then `onRealtimeEvent()`.
6. `backend/main.py` — how the two halves connect over HTTP.
7. Everything else, as needed, using [Section 7](#7-backend--file-by-file) and [Section 8](#8-frontend--file-by-file) as a reference/index rather than reading start to finish.

**The single most important fact about this codebase**, which explains almost every design decision in it:

> [!IMPORTANT]
> The AI model never decides what is true. It only ever transcribes/parses what the caller said into a clean value and hands it to the code. A plain Python **state machine** (`state_machine.py`) decides the next question, the price, the plan name, and the exact sentence to speak. The model is told to say that sentence, not to compose its own.

Every "why is it built this way" question in this document traces back to that one sentence.

---

## 2. What This Project Is

A voice bot that replaces a multi-step web quote *form* with a phone-call-like conversation. The caller talks (or types/taps, as a fallback), the bot asks the same questions the form used to ask, and a lead lands in the same place at the end — email / CRM webhook / local file. Same data, same downstream outcome, different front door.

It is a **standalone widget + API**. It does not touch the client's existing WordPress site or plugin. It gets embedded via one `<script>` tag wherever it's placed; the old manual form stays untouched as a fallback the whole time.

| | |
|---|---|
| **Input** | Caller's voice, streamed live from the browser mic (or typed/tapped, as an always-available fallback) |
| **Voice engine** | OpenAI Realtime API (`gpt-realtime-2.1-mini`) — one continuous two-way audio connection per call |
| **Brain** | A deterministic state machine in plain Python — **not** the AI model — decides every question, price, and plan |
| **Output** | Spoken replies streamed back live, plus a lead delivered by email / CRM webhook / local file |
| **Guarantee** | The model can never invent a price, invent a plan, or skip a question. The worst it can do is mishear something and have to ask the caller to repeat themselves |

---

## 3. The Core Design Principle

**The problem with "just let the AI talk freely":** a general-purpose LLM speaking without constraints can hallucinate a price, invent a plan that doesn't exist, or misquote availability. For something that turns directly into a real sales lead and a real customer expectation, that failure mode is not acceptable — even occasionally.

**The fix used throughout this codebase:** the model is only ever allowed to do one of a small handful of things when it has a clear answer — the main one being to call a tool named `confirm_slot(field, value)`. It is **never** allowed to compose the next question itself, and it is never allowed to state a price itself. The *code* (`dialogue/state_machine.py`) decides what happens next, computes any price from a CSV file (never from the model's memory), and hands the model back an exact string to speak (`say_next`). The model's system instructions tell it to say that string as-is, not paraphrase it.

This narrows the AI's actual job down to one thing: **turning messy human speech into a clean, structured value.** It parses. It never decides what's true.

You'll see this principle enforced in three separate places, redundantly, on purpose:
- **Tool schema level** (`realtime_tools.py`'s `_confirm_slot_schema`) — for a multiple-choice question, the `value` parameter is a strict JSON `enum` of only the real option IDs. The model is not just told the right answer; it is *structurally incapable* of sending a value that isn't one of the real options.
- **Server-side validation** (`handle_confirm_slot`) — even if a bad value somehow got through, it's checked again against `_valid_options_for()` before it's ever written to `session.slots`.
- **Pricing** — the model is explicitly told in its tool descriptions it may only ever speak a price that appeared in a previous `get_plan_pricing` or `confirm_slot` tool *result* in the same conversation, never one recalled from memory or estimated.

---

## 4. Two Architecture Generations (important)

This project has been built twice. **A developer who only reads the code comments or the old `README.md` at the repo root will get a wrong picture of the current architecture** — those docs describe generation 1, but the code has since moved to generation 2. This section is the correction.

### Generation 1 — WebSocket Relay `[LEGACY — fallback only]`

```
mic → stream raw PCM ──► FastAPI WebSocket (/ws/realtime/{session_id}) ──► OpenAI Realtime API
                                        │
                          (this backend process is IN the audio path,
                           relaying every frame both directions)
```

This is what `backend/services/realtime_session.py` (`RealtimeSession` class, ~1130 lines) implements, and it's what the root `README.md` and `demo.md` describe as "the" architecture — **that description is now out of date.** It relayed every audio frame through this Python process twice (OpenAI → backend → browser, over WebSocket/TCP) instead of carrying it directly. That extra hop, combined with a hand-rolled jitter buffer standing in for what a real realtime transport does natively, was the direct cause of a "choppy/laggy audio" bug (a frontend comment describes it as sounding like broken-up, stuttering speech).

This path still exists and still works — it's kept specifically as a fallback for a network that blocks WebRTC. It is not used by the current frontend (`frontend/realtime-widget.js`).

> [!WARNING]
> If you're debugging a live call and looking at logs, and you see `RealtimeSession` or anything from `realtime_session.py`, you are almost certainly looking at the wrong file for current behavior.

### Generation 2 — WebRTC Direct `[CURRENT — primary path]`

```
Browser  ── WebRTC (audio, both directions, direct) ──►  OpenAI Realtime API
   │                                                              │
   │  (backend only mints a short-lived token, and                │
   │   hands back session config, once at call start)              │
   ▼                                                              ▼
FastAPI backend  ◄──────────── REST /tool-call, per turn ─────────┘
  (business logic only — audio never transits this process)
```

This is what `frontend/realtime-widget.js` implements (its own header comment explicitly documents this as "REPLACES the old manual-PCM-over-WebSocket relay"). Audio now flows **directly** between the browser and OpenAI over a real WebRTC peer connection — the backend is never in the audio path at all. The backend's only two jobs at call time are:

1. **Once, at call start:** mint a short-lived OpenAI "ephemeral" client token (`POST /api/session/{id}/realtime-token`) and hand back the session configuration object (`GET /api/session/{id}/realtime-session-config`) — instructions, tool schemas, turn-detection settings.
2. **Once per model tool call (or per UI tap):** run the actual business logic over a plain REST call (`POST /api/session/{id}/tool-call`) and return the result.

Everything about *what question gets asked, what price gets quoted, what counts as a valid answer* is unchanged from Generation 1 — it's the exact same `DialogueManager` / `plan_matcher` / `notify` / `calendar_service` functions, just invoked over a REST round-trip per turn instead of inline while relaying a socket.

> [!IMPORTANT]
> When someone says "the voice assistant," they mean Generation 2 (WebRTC). `frontend/realtime-widget.js` + `backend/main.py`'s WebRTC-path endpoints + `backend/services/realtime_tools.py` is the *entire* live-call surface you need to reason about. `realtime_session.py` and `audio-worklets.js` are legacy/dormant code that only matter if you're specifically working on the WebRTC-unavailable fallback path.

---

## 5. Current Architecture — Full Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│  BROWSER                                                                  │
│  frontend/index.html + style.css + realtime-widget.js                     │
│                                                                             │
│   ┌──────────────┐         ┌────────────────────────────┐                 │
│   │  RTCPeerConn  │◄═══════│  WebRTC audio, both ways,    │═══════►  OpenAI │
│   │  (pc)         │         │  direct, no relay            │         Realtime│
│   └──────────────┘         └────────────────────────────┘          API     │
│          │                                                                  │
│   ┌──────────────┐         data channel "oai-events"                       │
│   │  data channel │◄═══════════ JSON events, both ways ═══════════►         │
│   │  (dc)         │         (transcripts, tool calls, turn signals)         │
│   └──────────────┘                                                         │
└───────────────────────────────┬───────────────────────────────────────────┘
                                 │  REST (HTTPS), only at call start
                                 │  + once per tool call / UI action
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  BACKEND — FastAPI, backend/main.py                                       │
│                                                                             │
│   POST /api/session/start                    → creates SessionState        │
│   GET  /api/session/{id}                      → resume after refresh       │
│   POST /api/session/{id}/realtime-token       → mints OpenAI ephemeral key │
│   GET  /api/session/{id}/realtime-session-config → instructions+tools blob │
│   POST /api/session/{id}/tool-call            → runs ONE tool's logic      │
│   POST /api/session/{id}/log-usage            → stores token/cost usage    │
│   GET  /api/session/{id}/usage                → read back cost of a call   │
│   WS   /ws/realtime/{id}                       → LEGACY relay (fallback)   │
│                                                                             │
│   Every /tool-call routes to:                                              │
│   backend/services/realtime_tools.py :: call_tool()                        │
│          │                                                                  │
│          ▼                                                                  │
│   backend/dialogue/state_machine.py :: DialogueManager                     │
│   (the FSM — decides next stage, computes say_next, owns all prices)        │
│          │                                                                  │
│          ├──► dialogue/slots.py           (static question definitions)    │
│          ├──► services/extraction.py      (messy text → clean value)       │
│          ├──► services/plan_matcher.py    (CSV-backed pricing/availability)│
│          ├──► services/notify.py          (lead delivery: WP→SMTP→file)    │
│          ├──► services/calendar_service.py(optional Google Calendar)       │
│          ├──► services/call_service.py    ("call me now" / Twilio)         │
│          └──► services/session_store.py   (SQLite persistence, all of it)  │
└─────────────────────────────────────────────────────────────────────────┘
```

**Two independent request/response contracts you must not conflate:**

1. **Browser ⟷ OpenAI** — raw Realtime API events (`response.create`, `response.done`, `conversation.item.created`, `input_audio_buffer.*`, etc.) over the WebRTC data channel. This is OpenAI's own event protocol; `frontend/realtime-widget.js`'s `onRealtimeEvent()` is the single switch statement that handles all of it.
2. **Browser ⟷ backend** — this project's own small REST API (`/tool-call`, `/realtime-token`, etc.), defined in `backend/main.py`, documented above and in [Section 7](#7-backend--file-by-file). This is where you add anything backend-specific.

A model tool call arrives over contract #1 (as a `response.function_call_arguments.done` event), and the frontend translates it into contract #2 (a `POST /tool-call` with `{name, arguments}`) — see `handleFunctionCall()` in `realtime-widget.js`. A UI action (tapping an option card, the Back button, etc.) is translated into the *same* `{name, arguments}` shape before hitting the same endpoint — see `callTool()`. This is deliberate: the backend's tool-call endpoint has exactly one code path for "the FSM needs to advance," regardless of whether a human tapped a button or the model called a function.

---

## 6. Repository Map

```
ac-quote-voice-assistant/                    (this repo, branch: s2s-realtime)
├── Dockerfile                                container build (python:3.12-slim)
├── Procfile                                  Railway/Heroku-style process file
├── README.md                                 project README (describes Generation 1 primarily — see §4 for the correction)
├── demo.md                                   demo/review notes from an earlier architecture migration (predates this document)
├── backend/
│   ├── main.py                               FastAPI app — every HTTP/WS endpoint
│   ├── config.py                             every tunable setting, one place, loaded from .env
│   ├── models.py                             SessionState, ManualInput (Pydantic)
│   ├── requirements.txt
│   ├── .env.example                          template for local .env (see §13 for drift vs config.py)
│   ├── quote_assistant.sqlite3               local dev DB (created automatically)
│   ├── leads_fallback.jsonl                  local file leads fall back to if WP+SMTP both fail
│   ├── service_account.json                  Google service account key (if Calendar is configured)
│   ├── stt_debug/                            captured raw audio + metadata from earlier STT debugging —
│   │                                          safe to ignore/delete, not read by any current (Gen 2) code path
│   ├── data/
│   │   ├── plans.csv                          plan catalog: id, name, price, period, features
│   │   ├── plan_availability.csv               category+tonnage+location → which plan_ids apply
│   │   └── plans_config.json                   (see plan_matcher.py — check current usage)
│   ├── dialogue/
│   │   ├── slots.py                            static question list (11 lines of actual content)
│   │   └── state_machine.py                    THE FSM — stage transitions, every spoken template
│   ├── services/
│   │   ├── realtime_session.py                 LEGACY — Gen 1 WebSocket relay session manager
│   │   ├── realtime_tools.py                   Gen 2 tool schemas + handlers (the model's API surface)
│   │   ├── extraction.py                       speech/text → clean value parsing
│   │   ├── plan_matcher.py                     CSV-backed plan/pricing lookup, mtime-cached
│   │   ├── notify.py                           lead delivery chain: WP endpoint → SMTP → JSONL file
│   │   ├── session_store.py                    all SQLite access — sessions, leads, usage, bookings, API keys
│   │   ├── calendar_service.py                 optional Google Calendar event creation
│   │   └── call_service.py                     "call me now" — urgent email always, optional Twilio call
│   └── tests/                                  pytest suite — no API key or network needed (see §14)
└── frontend/
    ├── index.html                              widget markup, no build step, no framework
    ├── style.css                               all styling
    ├── realtime-widget.js                       THE current frontend — WebRTC transport, ~1300 lines
    └── audio-worklets.js                        LEGACY — AudioWorkletProcessors, only used by the Gen 1 relay path
```

---

## 7. Backend — File by File

### `backend/main.py` (379 lines) — the FastAPI app

Owns every HTTP/WebSocket endpoint and session lifecycle. Read top to bottom, the important pieces are:

- **`SESSIONS: dict[str, SessionState]`** — the live, in-memory session table. Fine for a single-process demo/pilot deployment; the code comment explicitly flags this needs to move to Redis before running more than one backend process (a second process would have a disjoint `SESSIONS` dict and randomly 404 half of all requests for a session created on the other process).
- **`SESSION_LOCKS: dict[str, asyncio.Lock]`** — one `asyncio.Lock` per live call. A model-driven tool call and a UI button tap can both arrive as separate `/tool-call` HTTP requests and race to read-mutate-write `session.stage`. This lock serializes them per-session. This is the WebRTC-era descendant of `RealtimeSession._stage_lock` from the old relay path — same bug, same fix, now enforced across HTTP requests instead of asyncio tasks within one process.
- **`_check_api_key()` vs `_consume_api_key_once()`** — deliberately two different functions. `_check_api_key` only *validates* a key (safe to call many times: session creation, page-refresh resume). `_consume_api_key_once` actually *charges* one unit of quota, gated by `session.slots['_quota_charged']` so a browser reconnecting mid-call (dropped WebRTC, page refresh) can never double-charge. This whole mechanism is inert unless `REQUIRE_API_KEY=true` in config — off by default for the current single-client deployment.
- **Endpoints, in the order a call actually uses them:**

| Method & path | Purpose |
|---|---|
| `POST /api/session/start` | Creates a `SessionState`, returns `session_id` + first question. No audio/TTS here — the greeting is spoken live by the model once the realtime connection opens. |
| `GET /api/session/{id}` | Resume after a page refresh — returns current stage/slots/UI. |
| `POST /api/session/{id}/realtime-token` | **Gen 2 only.** Mints a short-lived OpenAI ephemeral client token so the browser can open its own `RTCPeerConnection` straight to OpenAI. The real `OPENAI_API_KEY` never reaches the browser. |
| `GET /api/session/{id}/realtime-session-config` | **Gen 2 only.** Returns the `session.update` payload (instructions, tools, turn detection) the browser sends to OpenAI once its data channel opens, plus the greeting line for the very first `response.create`. |
| `POST /api/session/{id}/tool-call` | **Gen 2 only, and the most important endpoint in the file.** Runs exactly one tool call's business logic via `realtime_tools.call_tool()`, wrapped in the per-session lock. Charges quota here (first point in the WebRTC flow that represents a real, in-progress call). Also contains a specific bug-fix (documented inline) for a "stuck after arrange a call" issue: if a tool call is rejected with no `say_next`, this endpoint now re-grounds the caller in the *actual* current question so they always hear something instead of dead air. |
| `POST /api/session/{id}/log-usage` | **Gen 2 only.** Receives the `usage` block off `response.done` from the browser's own data channel and stores it via `session_store.log_realtime_usage`. This exists because, on the WebRTC path, the backend has no other way to see token usage — audio and events never transit it. |
| `GET /api/session/{id}/usage` | Reads back the summed cost/token breakdown for one call — see [Section 12](#usage--cost-tracking) for how to use this. |
| `WS /ws/realtime/{id}` | **LEGACY (Gen 1).** The old full audio relay, kept only as a fallback if WebRTC is ever unavailable on a given network. Not used by the current frontend. |
| `POST /api/admin/keys` | Issues a new client API key (multi-tenant billing gate), protected by `ADMIN_API_SECRET`. Your endpoint to call when onboarding a customer — not exposed to them. |
| `GET /api/health` | Trivial liveness check + whether `OPENAI_API_KEY` is configured. |

- **`app.mount("/", StaticFiles(directory="../frontend", html=True))`** at the very bottom — this is *also* how the frontend gets served. There's no separate frontend server/build; FastAPI serves `frontend/` directly as static files at the same origin, which is also why the frontend's `API_BASE_URL` is simply `window.location.origin`.

### `backend/config.py` (117 lines) — every tunable knob, one place

All values load from environment variables via `os.environ.get(...)` with a hardcoded fallback default, so a missing `.env` file silently falls through to these defaults rather than crashing. See [Section 12](#12-configuration-reference) for the full table with rationale for every non-obvious value.

> [!WARNING]
> Two things worth knowing before you go read it — both are tracked in full as [Issue 1](#13-known-issues-drift--technical-debt) and [Issue 2](#13-known-issues-drift--technical-debt) in §13:
> 1. **`REALTIME_CALL_END_GRACE_SECONDS` is defined twice** (once near the top at `2.0`, once again near the bottom at `4.0`) — the second one silently wins in Python. Real drift, not intentional.
> 2. **`REALTIME_MODEL` here (`gpt-realtime-2.1-mini`) does not match `.env.example`'s value (`gpt-realtime-mini`)**. `config.py` is the actual source of truth for what a fresh checkout with no `.env` runs — `.env.example` is stale.

### `backend/models.py` (20 lines)

Two Pydantic models:
- **`SessionState`** — the entire state of one call: `session_id`, `stage`, `slots` (dict of everything collected so far), `available_plans`, `partial_inputs` (used for the phone-number split-across-turns case), `voice_fail_count`, `resume_stage` (used by the edit-then-resume flow), `created_at`.
- **`ManualInput`** — trivial `{field, value}` shape for typed/tapped answers.

### `backend/dialogue/slots.py` (80 lines)

Static definition of every **fixed-order** question: `full_name → phone → email → street → city → zip → category → tonnage → location → call_timing`. Each entry has a `prompt` (what to say), a `kind` (`text` / `phone` / `email` / `zip` / `choice`), and for `choice` kinds, the exact `options` list with `value`/`label` pairs. `ORDER` at the bottom is the linear backbone the FSM walks — branching stages (`category`, `location`, `plan_action`, `call_timing`) override this with logic in `state_machine.py`, everything else just advances to the next item in `ORDER`.

**Dynamic** stages — `plan_choice`, `plan_action`, `review_summary`, `schedule_appointment` — are *not* here, because their options depend on session data (which plans are actually available for this caller). Those are computed on the fly in `state_machine.py::_stage_meta()`.

### `backend/dialogue/state_machine.py` (571 lines) — the FSM, the actual product

The single most important file in the repository. `DialogueManager` owns all stage transitions for one session. **Every spoken line the bot ever says comes from a plain Python string template in this file — never from the model.** This is what makes the "can't hallucinate a price" guarantee mechanically true rather than just a prompt instruction.

Key methods, in the order you'll actually touch them:

| Method | What it does |
|---|---|
| `greeting()` | Sets stage to `full_name`, returns the opening line. |
| `handle_turn(transcript)` | Entry point for a raw voice transcript (Gen 1 path) — checks for meta-intent (go back, repeat, edit) first, then runs `extraction.extract()`, then `_apply_value()`. |
| `handle_manual(field, value)` | Entry point for an already-clean value (typed input, or a value the S2S model already parsed via `confirm_slot`) — this is the one `realtime_tools.py` calls. |
| `_handle_phone_turn` / `_handle_phone_digits` | Phone-specific: a caller's number can get split across two turns (recorder cuts off after 8-9 digits, the rest comes on the next turn). This tries the newly-said digits alone first (in case the caller just repeated the *whole* number), and only falls back to gluing onto a stored fragment if that fails — has two specific documented bug fixes for digit-gluing edge cases, worth reading in full if you ever touch phone handling. |
| `_apply_value(stage, value)` | Stores the value, resets `voice_fail_count`, checks whether this was a mid-flow *edit* (via `resume_stage`) that should snap back to where the caller was, then calls `_advance()` and `_entry_text()` for the next stage. |
| `_advance(stage, value, resume)` | The actual branching logic: `category` → skips straight to `closing` if not `cooling_electric_heat` (no instant plans for heating/heat-pump categories yet); `location` → looks up `plan_matcher.get_available_plans()` and routes to `closing` if none exist; `plan_action` → branches to `call_timing` or `schedule_appointment`; `call_timing=immediate` → fires `call_service.trigger_immediate_call` **in a background thread** (documented bug fix — this used to block the closing line on an up-to-20s network chain). |
| `_entry_text(stage)` | Builds the `(display_text, speech_text)` pair for whatever stage the FSM just landed on — including the full `review_summary` recap and the branching `_closing_text()`. |
| `go_back()` / `jump_to(field)` | Step back one stage, or jump directly to re-ask an already-answered field (used by both the "Change" button in the UI and the model's `go_back_or_edit` tool). `jump_to` sets `resume_stage` so a personal-info-field edit snaps back to where the caller was afterward — but a `category`/`tonnage`/`location` edit intentionally does **not** resume, since those legitimately need to re-walk the plan-selection chain. |
| `restart()` | Full session wipe back to `full_name`, same `session_id`. |
| `_recheck_plans_or_ask(ask_stage)` | Called when tonnage/category is corrected mid-review: tries the caller's *existing* location/plan against the new value first, only falls back to re-asking if that combo genuinely has no plans — stops an unrelated correction from forcing an unnecessary re-ask. |
| `_submit_lead_once()` | The single source of truth for "has this lead been emailed yet" — both the FSM's own natural progression to `closing` AND the model's explicit `save_lead_to_db` tool check the exact same `session.slots['_lead_submitted']` flag, so a lead can never be sent twice no matter which path reaches `closing` first. Backgrounded in a thread for the same reason as the immediate-call trigger above. |

### `backend/services/realtime_tools.py` (620 lines) — the model's entire API surface

This is the boundary between "the AI model" and "the real system," and the second most important file to understand deeply. It wraps the *exact same* `DialogueManager` / `plan_matcher` / `notify` / `session_store` / `calendar_service` functions the FSM already exposes — nothing about pricing, validation, or stage order is reimplemented here. This file only adds the tool-calling surface plus guard rails a raw function call doesn't give you for free.

- **`TOOL_SCHEMAS`** — the fixed list of tools that *can* exist: `get_plan_pricing`, `go_back_or_edit`, `save_lead_to_db`, `schedule_appointment`. (`confirm_slot` is deliberately *not* in this static list — see below.)
- **`_confirm_slot_schema(session)`** — builds the `confirm_slot` tool schema **fresh, every single stage**, locked to that exact stage. This is the fix for a real, documented bug: a static schema that let `value` be any free string forced the model to *invent* the underscore-slug option id from memory of the spoken prompt (e.g. guessing between `cooling_electric_heat` and `cooling_heat_pump`), and a wrong-but-valid-looking guess would pass validation and corrupt the FSM. Locking `field` to a one-value `enum` (the current stage only) and `value` to an explicit `enum` of the real option IDs (with human labels spelled out in the description) makes a wrong ID close to structurally impossible instead of merely discouraged.
- **`tools_for_stage(session)`** — stage-gated tool exposure. `confirm_slot` is explicitly excluded on the `schedule_appointment` stage — a documented bug fix, since offering it there gave the model an easy wrong shortcut (`confirm_slot(field='schedule_appointment', value='today, 18:00')`) that stores a raw string, closes the call, and skips the actual booking/calendar-event/conflict-check logic in `handle_schedule_appointment`.
- **`handle_confirm_slot(session, field, value, call_timing=None)`** — the actual handler. Guards: `field` must equal the caller's real current `session.stage` (prevents a stray/wrong tool call from silently jumping the FSM forward — editing an earlier field must go through `go_back_or_edit` first, which moves `session.stage` *before* `confirm_slot` runs); phone gets the digit-accumulation handling; `plan_choice` accepts a comma-separated multi-select and validates every id; there's a specific `_is_greeting_filler()` check that rejects "hi"/"hello"/etc. as a `full_name` value (a real observed failure mode — the model treating its own greeting turn as if it were the caller's answer).
- **`handle_schedule_appointment(session, call_date, call_time)`** — the real server-side backstop for AI-computed dates. The model is allowed to resolve "next Tuesday" or "tomorrow" into an exact `YYYY-MM-DD` itself (using the current date given in its instructions), but this function is what actually rejects a date in the past or beyond `config.SCHEDULE_MAX_DAYS_AHEAD` — a model date-math mistake must never be trusted blindly. Runs the Google Calendar event creation and the FSM's advance-to-closing call on **separate thread-pool futures** (`_SCHEDULE_POOL`) so their network latency overlaps instead of stacking sequentially — another documented fix for dead-air during scheduling.
- **`call_tool(session, name, arguments)`** — the dispatch table (`_DISPATCH`) and the single try/except boundary; any unhandled exception in a handler becomes a clean `{"ok": False, "error": "internal error..."}` instead of a 500 that could hang the caller.

### `backend/services/extraction.py` (388 lines) — messy input → clean value

Pure functions only — regex and string rules, zero network calls, zero LLM calls. Cannot hallucinate a price or plan name because prices/plans never pass through it at all (see `plan_matcher.py`/`state_machine.py` for those). Key entry points:

- **`extract(stage, transcript, stage_meta)`** — the main dispatcher, routes to the right cleaner below based on the stage's `kind`.
- **`clean_name`, `clean_phone`, `clean_zip`, `clean_email`, `extract_digits`** — field-specific cleaners.
- **`match_choice(text, options)`** — fuzzy-matches free text against a fixed option list using token overlap, for choice-kind fields.
- **Spelling-hint handling** (`_normalize_inline_spelling_hints`, `_apply_spelling_hint`, `_collapse_spelled_letter_runs`) — handles a caller spelling out a name letter-by-letter ("d-a-v-i-d") or giving an inline correction ("Charlie with a l-y"), which speech transcription commonly mangles.
- **`ONES_WORDS` / `TEEN_WORDS` / `TENS_WORDS`** — a deliberately *wider* homophone-correction net than plain digit words (`for`→`4`, `to`/`too`→`2`, `ate`→`8`, etc.), safe to apply aggressively only in pure-digit contexts (phone/zip) where Whisper-style mishearing of digits-as-words is common.

### `backend/services/plan_matcher.py` (109 lines) — pricing, from two CSVs

Reads `data/plans.csv` (the catalog: one row per plan — id, name, price, period, features) and `data/plan_availability.csv` (one row per category+tonnage+location combo, listing which plan IDs apply and which actions — go/call/visit — are allowed for that combo). Splitting these two means a price only ever needs editing once, in one place — the old single-CSV format repeated the full price/feature text on every availability row, which is exactly what made it easy to update only half the rows and get a silent price mismatch.

`_load()` checks both files' mtimes on every call and rebuilds the in-memory cache automatically if either changed — **no server restart or explicit `reload()` call is needed after editing a CSV.** `reload()` still exists as a no-op for any caller that still invokes it explicitly.

**To change a price:** edit `plans.csv`. **To change which plans are offered for a given setup:** edit `plan_availability.csv`. That's the entire pricing-change workflow — no code changes needed for either.

### `backend/services/notify.py` (253 lines) — lead delivery

Tries, strictly in this order, and always returns cleanly (a misconfigured `.env` must never crash a request):

1. **`WP_SUBMIT_ENDPOINT`** — POSTs straight to the client's existing WordPress form endpoint, so their current email templates/CRM hooks keep firing completely unchanged.
2. **Direct SMTP** — if WP isn't configured (or fails).
3. **Local `leads_fallback.jsonl` file** — if neither of the above is configured/working, so a lead is never silently lost even in a broken/demo environment.

`submit_lead(session, urgent=False)` is the normal end-of-call path. `submit_partial_lead(...)` is used by `main.py`'s background abandoned-lead sweeper (`_abandoned_lead_sweeper`, started on app startup) — recovers a lead from a caller who went idle and never reached `closing`, after `config.ABANDONED_LEAD_IDLE_SECONDS`.

### `backend/services/session_store.py` (523 lines) — all SQLite access

Deliberately plain `sqlite3` from the standard library — good enough for a demo/pilot without adding infrastructure; the module docstring explicitly notes the same function-level API could be backed by Postgres/Redis later with minimal changes to `main.py`. See [Section 11](#11-database-schema) for the full schema. Public functions map roughly 1:1 to what's in the schema — `save_session`/`load_session`, `save_lead_snapshot`, `log_transcript_turn`/`get_transcript`, `log_realtime_usage`/`get_usage_summary`, `book_call_slot`/`is_slot_booked`/`get_booked_slots_in_range`, `issue_api_key`/`check_api_key`/`check_and_increment_api_key`.

### `backend/services/calendar_service.py` (131 lines) — optional Google Calendar

`create_call_event(session, call_date, call_time)` — wrapped so a missing/misconfigured service account **never** breaks lead submission or slot booking (same graceful-degradation pattern as `notify.py`'s fallback chain). Returns `None` on any failure or missing config; the booking still saves to `booked_slots` and the lead still gets emailed regardless.

> [!WARNING]
> **A bare Google service account cannot invite attendees** (`sendUpdates="all"` with an `attendees` list) without domain-wide delegation configured in Google Workspace Admin. Without that extra one-time setup step (`GOOGLE_DELEGATED_USER` in `.env`), the event is still created and visible on the team's calendar, but the *customer* is silently never emailed an invite — this fails with no crash and no visible symptom other than "the customer says they never got an email." `_closing_text()` in `state_machine.py` checks `_calendar_attendee_invited` before promising the caller a calendar email, specifically so the bot never promises something that didn't happen. This is documented at the top of `calendar_service.py` too — read it before anyone asks why a customer didn't get an invite.

### `backend/services/call_service.py` (97 lines) — "call me right now"

Two layers, same graceful-degradation pattern again:
1. **Always:** an urgent lead email (`notify.submit_lead(urgent=True)`) — guaranteed to fire with zero external dependencies. This alone is what actually gets the customer called back even if nothing below is set up.
2. **Optional:** if Twilio is fully configured, places a real outbound call to the org's number, reads out the customer's details via TwiML, then dials the customer directly so the org can be bridged straight through.

`_to_e164()` normalizes the caller's bare 10-digit number (which is all `extraction.clean_phone()` ever returns) into full E.164 before Twilio dials it — without this, the org-side leg (already full E.164, typed into `.env` directly) connects fine while the customer-side leg silently fails to route, which is easy to miss because the first half of the flow still looks like it worked.

> [!NOTE]
> This file's own docstring flags it as untestable from a sandboxed dev environment with no outbound network access to `api.twilio.com` — it's written against Twilio's documented REST/TwiML shape but should be verified against a real trial account before being trusted in production.

### `backend/services/realtime_session.py` (1134 lines) `[LEGACY — Generation 1 only]`

`RealtimeSession` — owns one full WebSocket-relayed call: mic bytes in, OpenAI Realtime API in the middle, spoken audio out, all relayed through this Python process. Still fully functional, still covered by tests (`test_realtime_session_watchdog.py`), and still wired up at `/ws/realtime/{id}` in `main.py` — but not used by the current frontend, which speaks WebRTC directly to OpenAI instead (see [Section 4](#4-two-architecture-generations-important)). Only touch this file if you're specifically working on the WebRTC-unavailable fallback path.

> [!WARNING]
> If you're debugging a *live* call's voice behavior and you find yourself reading this file, stop — you almost certainly want `frontend/realtime-widget.js` instead.

---

## 8. Frontend — File by File

### `frontend/realtime-widget.js` (~1300 lines) — the entire current frontend

One file. No build step, no bundler, no framework — plain DOM APIs and `fetch`. Its own header comment is the best possible summary of what it does and why; read it before anything else in this section.

**Mental model:** this file has two halves that mostly don't know about each other:
1. **UI half** — rendering the transcript, option cards, the schedule-appointment calendar widget, the sidebar "readings" (answered-so-far fields), the mic button's visual state. Mostly straightforward DOM manipulation.
2. **Transport half** — everything from the `// ---- WebRTC transport ----` comment onward. This is the half that matters for debugging voice/latency/rate-limit issues, and the half most of this project's actual debugging history (see [Section 13](#13-known-issues-drift--technical-debt) and the git history) has been spent on.

**Key state variables** (all module-level, since this is one script with no framework state management):

| Variable | Purpose |
|---|---|
| `pc` | The `RTCPeerConnection` — carries audio directly to/from OpenAI. |
| `dc` | The data channel (`"oai-events"`) — carries the same JSON events the old WS relay used, just over WebRTC's data channel instead of a plain WebSocket. |
| `micTrack` | The local mic's audio track. Muted/unmuted **in place** (`micTrack.enabled = true/false`) rather than gating raw PCM bytes the way the old relay path did — there is no client-side VAD/silence gate on this path anymore; turn detection is entirely OpenAI's server-side job (`semantic_vad` or `server_vad`, configured via `realtime-session-config`). |
| `responseActive` / `pendingResponses` | A response-creation queue. Up to six different triggers (a tool call finishing, a UI button tap, typed text, push-to-talk release, OpenAI's own VAD, a retry) can each want to start a spoken response at the same moment — OpenAI rejects a second `response.create` while one is already in flight. Queuing instead of firing immediately, with a staleness check (a queued entry only fires if `session.stage` still matches the stage it was queued under), is the fix. |
| `conversationItemIds` / `MAX_KEPT_EXCHANGES` | Client-side conversation-history trimming. Deletes conversation items older than the last `MAX_KEPT_EXCHANGES` user turns, **only** after a response fully completes successfully (never mid-response, never during a retry). This exists purely to control per-turn token cost (see [Section 13](#13-known-issues-drift--technical-debt) — this was the direct fix for a real TPM rate-limit problem hit during testing). The real state of what's been collected lives in `session.slots` on the backend, not in the model's context, so the model never actually needs old turns in-context to know what's already been answered. |
| `RESPONSE_WATCHDOG_MS` / `armResponseWatchdog()` | A 2500ms inactivity timer (reset on every transcript delta) that force-cancels and retries a response that's gone silent mid-flight — most commonly caused by a noise/echo blip crossing the server VAD threshold while the bot is mid-question. |
| `activeResponseInstructions` / `MAX_RESPONSE_RETRIES` / `RESPONSE_RETRY_DELAY_MS` / `RESPONSE_RETRY_DELAY_CAP_MS` | Retry bookkeeping for a failed/cancelled response. Retries resend the *same* scripted line (never a blank/model-decided turn) and are capped, both in count (`MAX_RESPONSE_RETRIES = 2`) and in delay (exponential, hard-capped — see [Section 13](#13-known-issues-drift--technical-debt) for why the cap matters and what it used to be before a fix). |
| `safeSend(obj)` | Every single `dc.send(...)` in the file goes through this wrapper, which checks `dc.readyState === "open"` before sending. Without this guard, a send that fires after the data channel has already closed (a watchdog-forced reset, a stale retry timer, the call ending) throws an **uncaught `InvalidStateError`** and kills the tab outright — this was a real, reproduced crash, fixed by routing every send through this one chokepoint. |
| `lastTokenRateLimit` | Captured from OpenAI's `rate_limits.updated` event — used for diagnostics/logging, not for sizing any retry delay (a live call cannot afford to wait out OpenAI's full per-minute reset window; see [Section 13](#13-known-issues-drift--technical-debt)). |

**Key functions, roughly in call order for one live call:**

| Function | Role |
|---|---|
| `startSession()` | `POST /api/session/start`, gets a `session_id`, kicks off `connectWebRTC()`. |
| `connectWebRTC()` | The full WebRTC handshake: fetches an ephemeral token + session config from the backend in parallel, grabs the mic (muted immediately), creates the `RTCPeerConnection` + data channel, does the SDP offer/answer exchange directly against `https://api.openai.com/v1/realtime/calls`, and on `dc.onopen` sends the initial `session.update` and fires the greeting's `createResponse()`. |
| `onRealtimeEvent(event)` | The single giant switch over every event type OpenAI's data channel sends — transcript deltas, `response.done`, `conversation.item.created`, `rate_limits.updated`, function-call events, errors. This is the correct starting point for tracing *any* live-call behavior question. |
| `handleFunctionCall(event)` | Translates a `response.function_call_arguments.done` event into a `POST /tool-call`, then feeds the result back via `respondToToolResult()`. |
| `callTool(name, args)` | Same REST call as above, but for UI-driven actions (option tap, Back, Change, schedule widget) — translates a tap into the same `{name, arguments}` shape a model tool call would produce, so the backend has exactly one code path for "the FSM needs to advance" regardless of who triggered it. Also resends the tool list (and, per stage, turn-detection config) via `session.update` whenever the stage actually changed. |
| `createResponse(instructions, isRetry)` | Queues (or immediately fires, if nothing's active) a `response.create`, tracking `activeResponseInstructions` for potential retry. |
| `trimConversationHistory()` | The token-cost control described above — runs after every successfully completed response. |
| `attemptReconnect()` | Exponential-backoff WebRTC reconnection (`RECONNECT_BASE_DELAY_MS`, capped at `MAX_RECONNECT_ATTEMPTS`) if the peer connection drops unexpectedly (not on a deliberate close). |
| `endCall(reason)` / `scheduleCallEnd(reason)` | Client-side call termination — idle timeout and max-duration hard cap are enforced here via `idleCheckInterval` (see `IDLE_TIMEOUT_MS` / `MAX_CALL_MS`, mirroring `config.py`'s server-side `REALTIME_IDLE_TIMEOUT_SECONDS` / `REALTIME_MAX_CALL_SECONDS` on the legacy path — on the WebRTC path there is no server-side enforcement of these, since the backend isn't in the call at all; this is now a purely client-side safety net). |

### `frontend/index.html` (82 lines)

Plain markup, no templating. Structure: a header (brand + Replay/New buttons), a `.conversation-panel` (the circular dial/mic button SVG, connection status text, the live transcript, and a `#stage-ui` div the JS injects option cards / text inputs / the schedule widget into), and a `.progress-rail` sidebar listing every collected field (`data-field="..."` attributes the JS looks up by name to fill in live as the call progresses).

### `frontend/style.css` (443 lines)

All styling for the above — no CSS framework, no preprocessor. Organized roughly by component (dial/mic button, transcript, option cards, schedule widget, progress rail).

### `frontend/audio-worklets.js` (144 lines) `[LEGACY — Generation 1 only]`

Two `AudioWorkletProcessor` classes (`MicCaptureProcessor`, `PlaybackProcessor`) that replaced two older `ScriptProcessorNode`s on the WebSocket-relay path — moving audio capture/playback off the main thread (onto a dedicated realtime audio thread) so main-thread contention (GC pauses, DOM work) couldn't stall audio. **Entirely unused on the current WebRTC path** — WebRTC handles jitter buffering and audio scheduling natively, so there's no manual PCM capture/playback loop left to move off the main thread in the first place. This file is dead code relative to the current frontend; it only matters if you're working on the `/ws/realtime` fallback path.

---

## 9. The Finite State Machine (FSM)

The FSM is the actual product. Every other file exists to either (a) get a clean value into it, or (b) speak what it produces. `dialogue/slots.py` defines the *fixed* stages; `dialogue/state_machine.py::_advance()` and `::_stage_meta()` handle the *branching* ones.

```
                    ┌───────────┐
                    │ full_name │
                    └─────┬─────┘
                          ▼
                    ┌───────────┐
                    │   phone   │  (digits may arrive split across 2 turns)
                    └─────┬─────┘
                          ▼
                    ┌───────────┐
                    │   email   │
                    └─────┬─────┘
                          ▼
                    ┌───────────┐
                    │  street   │
                    └─────┬─────┘
                          ▼
                    ┌───────────┐
                    │   city    │
                    └─────┬─────┘
                          ▼
                    ┌───────────┐
                    │    zip    │
                    └─────┬─────┘
                          ▼
                    ┌───────────┐
                    │ category  │──── NOT cooling_electric_heat ────► closing
                    └─────┬─────┘     (no instant plans for heating /
                          │            heat-pump categories yet)
                (cooling_electric_heat)
                          ▼
                    ┌───────────┐
                    │  tonnage  │
                    └─────┬─────┘
                          ▼
                    ┌───────────┐
                    │ location  │──── zero plans found for this ────► closing
                    └─────┬─────┘     category+tonnage+location combo
                          │
                (plans found — plan_matcher.get_available_plans)
                          ▼
                  ┌───────────────┐
                  │  plan_choice   │  (multi-select — comma-separated plan ids)
                  └───────┬───────┘
                          ▼
                 ┌─────────────────┐
                 │ review_summary   │──── caller wants a change ──┐
                 └────────┬────────┘                             │
                          │ confirmed                             │
                          ▼                                       │
                  ┌───────────────┐         (jump_to any field,   │
                  │  plan_action   │          resumes back here    │
                  └───────┬───────┘          for personal-info     │
              ┌───────────┼──────────┐        fields only) ◄───────┘
    go_with_plan     arrange_call   arrange_visit
              │           │              │
              ▼           ▼              ▼
          closing   ┌───────────┐  schedule_appointment
                     │call_timing│  (appointment_type="visit")
                     └─────┬─────┘         │
                  immediate│ │scheduled     │
                     ┌─────┘ └──────┐       │
                     ▼              ▼       ▼
                  closing    schedule_appointment
                             (appointment_type="call")
                                    │
                                    ▼
                                 closing
```

**Meta-intents, available at (almost) any stage, handled before normal field extraction:**
- *"go back" / "previous"* → `go_back()` — one stage back, nothing lost.
- *"start over" / "restart"* → `restart()` — full wipe, same `session_id`.
- *"repeat" / "say that again"* → re-reads the current stage's entry text, nothing changes.
- *"change my email" / "fix the zip code"* (edit-verb + field-synonym match, see `_find_edit_field`) → `jump_to(field)` — re-asks that field, and if it's a personal-info field (`full_name`/`phone`/`email`/`street`/`city`/`zip`), remembers to resume back to wherever the caller was afterward via `resume_stage`. Branching fields (`category`/`tonnage`/`location`) do **not** resume — an edited category legitimately needs to re-walk tonnage/location/plan-selection, since those depend on it.

---

## 10. A Call, Start to Finish (step-by-step trace)

This section traces one complete call through every file involved, in the exact order things happen. Use this when you need to answer "where does X actually happen" for any point in a call's lifecycle.

1. **Page load.** Browser requests `/`, FastAPI's `StaticFiles` mount serves `frontend/index.html` directly (same origin as the API — no separate frontend host, no CORS to configure for this).
2. **`startSession()` fires** (frontend). `POST /api/session/start` → `main.py` creates a `SessionState`, calls `DialogueManager(session).greeting()` (sets stage to `full_name`, returns the opening line), saves initial progress, returns `session_id` + the first question + UI shape. Frontend stores `session_id` in `sessionStorage` (`ac_quote_session_id`) so a page refresh can resume.
3. **`connectWebRTC()` fires** (frontend). Two parallel requests: `POST /realtime-token` (mints an OpenAI ephemeral client key — `main.py` calls OpenAI's `/v1/realtime/client_secrets`) and `GET /realtime-session-config` (returns the full `session.update` payload — instructions, tool schemas for the current stage, turn-detection config — built by `build_session_dict()` in `realtime_session.py`, reused as a plain dict by the WebRTC path even though that file's *class* is Gen-2-only). Mic is grabbed and **immediately muted** before being added as a track — the mic must never be "hot" before the app deliberately unmutes it.
4. **SDP handshake.** Browser creates an `RTCPeerConnection` + a data channel named `"oai-events"`, generates an SDP offer, POSTs it directly to `https://api.openai.com/v1/realtime/calls` (authenticated with the ephemeral key, **not** the browser ever touching the real API key), gets back an SDP answer, sets it as the remote description. Audio now flows directly browser ⟷ OpenAI.
5. **`dc.onopen` fires.** The browser sends the full `session.update` it fetched in step 3, then immediately calls `createResponse(greeting_instructions)` — this is the bot's first spoken line, and it's the *exact same string* `DialogueManager.greeting()` produced in step 2, just spoken through the live connection instead of only displayed as text.
6. **Caller speaks (or types/taps).** OpenAI's server-side turn detection (`semantic_vad` by default, or `server_vad` if configured per-stage) decides when the caller's turn ends, transcribes it, and — because the system instructions tell it to — calls a tool (almost always `confirm_slot`) rather than composing a free-text reply.
7. **Tool call arrives** as a `response.function_call_arguments.done` event on the data channel → `onRealtimeEvent()` → `handleFunctionCall()` → `POST /tool-call` with `{name: "confirm_slot", arguments: {field, value}}`.
8. **Backend processes the tool call**, under the per-session `asyncio.Lock`: `realtime_tools.call_tool()` → `handle_confirm_slot()` → validates `field` matches `session.stage`, validates `value` against the real options, calls `DialogueManager.handle_manual()` → `_apply_value()` → `_advance()` computes the next stage (possibly doing a plan lookup via `plan_matcher`, possibly firing a backgrounded email/SMTP/Twilio/Calendar call) → `_entry_text()` builds the next `(display, speech)` pair. The endpoint returns `{ok, say_next, stage, slots, ui, tools?}` — `tools` is only included if the stage actually changed (so the model's cached instructions block doesn't need re-sending, only the tool list).
9. **Backend result returns to the browser** → `respondToToolResult()` sends a `conversation.item.create` with the function result, then `createResponse(say_next)` (queued if another response is already active) — the bot speaks the *exact* string the FSM produced, and updates the sidebar UI (`updateReadings`) with whatever new value was just confirmed.
10. **Repeat steps 6-9** for every stage, with `trimConversationHistory()` clearing old turns after each successfully completed response to keep per-turn token cost roughly constant regardless of call length.
11. **Terminal stage reached (`closing`).** `_advance()` (or `handle_schedule_appointment` for the booking path) calls `_submit_lead_once()`, which fires `notify.submit_lead()` in a background thread — tries WP endpoint → SMTP → local JSONL file, in that order, and sets `session.slots['_lead_submitted']` so this can never double-fire regardless of which code path reached `closing`.
12. **Call ends.** Either the caller hangs up, or the client-side idle/max-duration timers in `realtime-widget.js` fire `endCall()`. On the WebRTC path there is no server-side call-duration enforcement (the backend was never in the audio path); billing safety is entirely the client-side `IDLE_TIMEOUT_MS`/`MAX_CALL_MS` timers plus whatever the caller's own tab does.
13. **Usage gets logged**, once per `response.done`, via `POST /log-usage` from the frontend straight off the data channel's own `usage` block — this is the *only* way the backend learns what a call cost on this path, since it was never in the audio/event stream. Check it after any test call with `GET /api/session/{id}/usage`.

---

## 11. Database Schema

SQLite, file at `config.APP_DB_PATH` (default `backend/quote_assistant.sqlite3`), created automatically on startup by `session_store.init_db()`. Six tables:

| Table | Key columns | Purpose |
|---|---|---|
| **`sessions`** | `session_id` (PK), `state_json`, `created_at`, `updated_at` | Full serialized `SessionState` — lets a call resume after a page refresh. |
| **`lead_snapshots`** | `session_id` (PK), `stage`, `slots_json`, `available_plans_json`, `is_complete`, `abandoned_notified_at` | Progress checkpoint saved after most tool calls — this is what the abandoned-lead sweeper (`main.py`'s `_abandoned_lead_sweeper`) reads to recover a lead from a caller who went idle without finishing. |
| **`realtime_transcripts`** | `id` (PK), `session_id`, `speaker`, `transcript`, `created_at` | Turn-by-turn transcript log (used on the legacy relay path; the WebRTC path's transcripts live client-side in the DOM and aren't currently persisted server-side turn-by-turn — see §13). |
| **`realtime_usage`** | `id` (PK), `session_id`, `input_tokens`, `output_tokens`, `total_tokens`, `input_cached_tokens`, `input_text_tokens`, `input_audio_tokens`, `output_text_tokens`, `output_audio_tokens` | One row per `response.done`, fed by `POST /log-usage`. `GET /api/session/{id}/usage` sums these into a cost estimate — see [Section 12](#usage--cost-tracking). |
| **`realtime_whisper_audio`** | `session_id` (PK), `bytes_sent` | Raw PCM byte count for the legacy relay path's separate Whisper transcription billing — not populated on the WebRTC path. |
| **`booked_slots`** | `id` (PK), `call_date`, `call_time`, `session_id`, `appointment_type`, `UNIQUE(call_date, call_time)` | Callback/visit bookings — the `UNIQUE` constraint is the actual race-condition guard against two callers double-booking the same slot (`book_call_slot` catches the resulting `IntegrityError` and returns `False` rather than crashing). |
| **`api_keys`** | `api_key` (PK), `client_name`, `plan_tier`, `monthly_quota`, `requests_used`, `period_start`, `active` | Multi-tenant API-as-a-service gating — inert unless `REQUIRE_API_KEY=true`. |

`init_db()` also runs simple `ALTER TABLE ... ADD COLUMN` migrations guarded by a `PRAGMA table_info` check (for `abandoned_notified_at` and `appointment_type`) — the closest thing this project has to a migration system. If you add a new column to an existing table, follow that same pattern rather than assuming a fresh `CREATE TABLE IF NOT EXISTS` will pick it up on an existing DB file.

---

## 12. Configuration Reference

Every setting lives in `backend/config.py`, loaded from environment variables via `python-dotenv`. **`config.py`'s hardcoded fallback defaults are the actual source of truth** for a fresh checkout with no `.env` file — treat `.env.example` as a starting template to copy, not as documentation of current defaults (see [Section 13](#13-known-issues-drift--technical-debt) for specific places the two have drifted apart).

### Voice / Realtime API

| Setting | Default (in `config.py`) | Why |
|---|---|---|
| `OPENAI_API_KEY` | *(required, no default)* | Without this, `/realtime-token` returns a 500 and the frontend shows "Could not start the voice engine." |
| `REALTIME_MODEL` | `gpt-realtime-2.1-mini` | Pinned to a specific snapshot on purpose, not an auto-updating alias — confirm the exact string in the OpenAI dashboard's model list before changing it. |
| `REALTIME_VOICE` | `alloy` | |
| `REALTIME_VAD_MODE` | `far_field` | This is a speakerphone bot (car, shop, kitchen), not a headset bot — `near_field` is tuned for a mic close to the mouth and will false-trigger on ambient room noise here. |
| `REALTIME_VAD_TYPE` | `semantic_vad` | Judges turn-end from *what was said* (linguistic completeness) rather than a flat silence timer. |
| `REALTIME_VAD_EAGERNESS` | `medium` | Moved up from `low` after a client speed complaint. `high` risks cutting a caller off mid-sentence — re-test carefully before pushing further. Only matters when `REALTIME_VAD_TYPE=semantic_vad`. |
| `REALTIME_VAD_THRESHOLD` | `0.7` | Only applies when `REALTIME_VAD_TYPE=server_vad` — currently dead with the default `semantic_vad` type. |
| `REALTIME_VAD_SILENCE_MS` | `800` | Only applies under `server_vad`. Raised from `700` after a real test call closed a turn right after "I" — before "...arrange a call" finished — because the pause landed exactly on the old threshold. |
| `REALTIME_STUCK_TURN_SECONDS` | `22` | **Legacy (Gen 1) path only** — `RealtimeSession._watchdog`'s forced-commit threshold. Raised from `12` after real calls showed normal think-time on multi-choice questions running 12-13 seconds. |
| `REALTIME_IDLE_TIMEOUT_SECONDS` | `120` | **Legacy path only** — server-side hang-up after this much total silence. |
| `REALTIME_MAX_CALL_SECONDS` | `300` | **Legacy path only** — absolute hard cap per call regardless of activity. |
| `REALTIME_CALL_END_GRACE_SECONDS` | `4.0` *(see §13 — defined twice)* | Lets the closing line finish playing before the connection is torn down. |

### Lead delivery / data

| Setting | Default | Notes |
|---|---|---|
| `WP_SUBMIT_ENDPOINT` | *(blank)* | First choice in `notify.py`'s delivery chain. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` | blank / `587` / blank / blank | Second choice. |
| `NOTIFY_EMAIL_TO` / `NOTIFY_EMAIL_FROM` | blank / falls back to `SMTP_USER` | |
| `LEADS_FALLBACK_FILE` | `leads_fallback.jsonl` | Third, always-available choice. |
| `APP_DB_PATH` | `quote_assistant.sqlite3` | |
| `ABANDONED_LEAD_IDLE_SECONDS` / `ABANDONED_LEAD_SWEEP_SECONDS` | `900` / `120` | How long a call must be idle before it's recovered as a partial lead, and how often the sweeper checks. |
| `CORS_ORIGINS` | `*` | Tighten before a real multi-tenant deployment. |

### Google Calendar (optional)

| Setting | Default | Notes |
|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_FILE` / `GOOGLE_SERVICE_ACCOUNT_JSON` | blank / blank | Blank = booking still works, calendar event just isn't created. If both are set, the JSON-contents var wins (needed for PaaS hosts like Railway that can't hold an arbitrary gitignored file path). |
| `GOOGLE_CALENDAR_ID` | `primary` | |
| `CALENDAR_TIMEZONE` | `America/Chicago` | **Note:** this is a US timezone default in a project whose test data/phone examples are Indian (`+91`) — confirm this matches the actual deployment region before relying on it; see §13. |
| `GOOGLE_DELEGATED_USER` | blank | Required for the customer to actually receive a calendar email invite — see `calendar_service.py`'s docstring for the full one-time Workspace Admin setup. Blank = event still created, customer not invited. |
| `SCHEDULE_MAX_DAYS_AHEAD` | `30` | Server-side backstop against a model-computed date being trusted blindly. |
| `SCHEDULE_BUSINESS_HOURS_START` / `_END` / `SCHEDULE_SLOT_MINUTES` | `09:00` / `18:00` / `30` | Drives the frontend's calendar/slot-picker widget. |

### API-as-a-service gating (off by default)

| Setting | Default | Notes |
|---|---|---|
| `ADMIN_API_SECRET` | blank | Master secret for `POST /api/admin/keys` — yours, never handed to a customer. |
| `REQUIRE_API_KEY` | `false` | Flip on once you're actually issuing separate keys to separate customers; changes nothing for the current single-client deployment while off. |

### Twilio (optional)

| Setting | Default | Notes |
|---|---|---|
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_FROM_NUMBER` / `ORG_PHONE_NUMBER` | all blank | Blank = urgent lead email still sends, just no automated dial. |
| `DEFAULT_COUNTRY_CODE` | `+91` | Used by `_to_e164()` to normalize a bare local number. |

### Usage / cost tracking

After any test call, don't guess what it cost — ask the backend:

```bash
curl http://localhost:8000/api/session/<session_id>/usage
```

To get `<session_id>` from a live browser tab mid- or post-call, open DevTools Console and run:

```js
sessionStorage.getItem("ac_quote_session_id")
```

This returns a breakdown of input/output tokens, split into cached vs. non-cached and text vs. audio (audio is the expensive side), plus an estimated dollar cost computed against the per-1M-token rates hardcoded in `session_store.py`'s `_RATES` dict — confirm those rates against OpenAI's current published pricing before trusting the dollar figure for real invoicing; it's explicitly an estimate, not a billing record.

---

## 13. Known Issues, Drift & Technical Debt

This section exists so the next developer doesn't have to rediscover these the hard way. None of these mean "the system is broken" — the system works — but each is a real, verified gap between what the code currently does and what it should, or a real risk if left alone. Every entry follows the same shape: what it is, why it matters, and the fix.

### Issue 1 — `REALTIME_CALL_END_GRACE_SECONDS` is defined twice in `config.py`
**Impact:** Cosmetic today, landmine tomorrow · **Fix:** delete the duplicate, keep one definition

Once near the top (`2.0`) in the voice-settings block, once again near the bottom (`4.0`) near the Twilio settings. Python silently keeps the second definition; the first is dead. Not currently causing a behavior bug — the intended active value, `4.0`, is what's actually in effect — but it's a landmine for anyone who edits the *first* definition expecting it to take effect.

### Issue 2 — `.env.example` has drifted from `config.py`'s actual defaults
**Impact:** Silent misconfiguration on a fresh environment · **Fix:** regenerate `.env.example` from current defaults, or add a test that diffs the two

`.env.example` shows `REALTIME_MODEL=gpt-realtime-mini` while `config.py`'s hardcoded fallback is `gpt-realtime-2.1-mini`; `.env.example` shows `REALTIME_VAD_EAGERNESS=low` and `REALTIME_VAD_SILENCE_MS=700` while `config.py`'s actual defaults are `medium` and `800` (with an inline comment in `config.py` explaining *why* it moved past what `.env.example` still shows). Anyone provisioning a new environment by copying `.env.example` verbatim gets the old, already-superseded tuning values, silently.

### Issue 3 — Rate-limit retry/backoff logic has a history of real bugs (now fixed)
**Impact:** Was a live-call crash risk · **Fix:** already shipped — read this before touching the code again

Found and fixed during testing, in order:
- **(a)** a *duplicate* `scheduleResponseRetry` function declaration in `realtime-widget.js`, where the second definition silently overrode a simpler, correct one and caused retries to wait for OpenAI's *entire* ~60-second TPM reset window instead of a short capped backoff — a live caller cannot sit through 60 seconds of silence.
- **(b)** every `dc.send(...)` call site lacked a `readyState` check, so a send firing after the data channel had already closed (from a watchdog-forced reset racing against a pending retry) threw an uncaught `InvalidStateError` and crashed the tab outright.
- **(c)** the watchdog's forced `response.cancel` fired unconditionally, even when nothing was actually active, producing a spurious `response_cancel_not_active` error from OpenAI.

All three are fixed as of this document (`safeSend()` helper, `RESPONSE_RETRY_DELAY_CAP_MS`, and a `responseActive` guard before cancelling).

> [!WARNING]
> If you see any of `InvalidStateError`, `response_cancel_not_active`, or a multi-second dead-air stall in a test call again, this is the exact place to look first. Issue 4 below (`trimConversationHistory` / `MAX_KEPT_EXCHANGES`) is the companion fix for the *root cause* of hitting the rate limit in the first place — not just the crash that hitting it used to cause.

### Issue 4 — Per-turn token cost and `MAX_KEPT_EXCHANGES`
**Impact:** Token cost growing with call length · **Fix:** already mitigated for growth; see below for the remaining floor

The FSM has roughly 10-14 stages total; a normal call previously never grew its conversation history enough to trigger trimming at any reasonable threshold, meaning every turn's full audio history stayed in-context and got fully reprocessed on every single `response.create` — token cost grew roughly linearly with call length instead of staying flat. `MAX_KEPT_EXCHANGES` (currently `2`) plus `trimConversationHistory()` fixes the *growth*.

> [!NOTE]
> If you're still hitting the OpenAI TPM ceiling on an early turn even with trimming, that means the **floor** itself — system instructions plus the current stage's tool schemas plus one audio exchange — is already close to the limit, and no amount of further trimming will help. The fix in that case is shrinking instructions/tool schemas per stage, not the trim window. Use `GET /api/session/{id}/usage` (§12) to check which case you're in before changing anything here.

### Issue 5 — The legacy WebSocket relay path hasn't received the WebRTC path's bug fixes
**Impact:** Real risk if the fallback is ever relied on in production · **Fix:** port Issue 3's fixes before trusting this path with real traffic

`realtime_session.py`, `audio-worklets.js`, and `/ws/realtime/{id}` are fully functional but completely unmaintained relative to the WebRTC path's recent bug-fix history. All of the rate-limit/retry/trim fixes in Issue 3 live in `realtime-widget.js` and have **not** been ported to `realtime_session.py`. If this fallback path is ever actually relied on (a network that blocks WebRTC), it will hit the same class of bugs the WebRTC path already fixed, with zero of those fixes present.

### Issue 6 — `CALENDAR_TIMEZONE` defaults to a US timezone in an India-facing deployment
**Impact:** Every scheduled appointment could be silently off by hours · **Fix:** set explicitly in `.env` — do not rely on the default

`CALENDAR_TIMEZONE` defaults to `America/Chicago`, while the rest of the project's defaults (`DEFAULT_COUNTRY_CODE=+91`, test fixtures using Indian phone numbers) point at an India-based deployment. If the real deployment is India-based, this default is almost certainly wrong and every scheduled appointment's date/time math (`handle_schedule_appointment`, `_schedule_ui`) will be silently off by the timezone difference.

### Issue 7 — Root `README.md` and `demo.md` describe Generation 1 as primary
**Impact:** Outdated mental model for a new reader · **Fix:** point both files at this document, or update them directly

They are not wrong about anything they say — the relay path they describe is real and still works — but they were written before the WebRTC migration and give a new reader an outdated picture of what's actually live. This document (`ARCHITECTURE.md`) supersedes them for architecture purposes.

### Issue 8 — `realtime_transcripts` is not populated on the current WebRTC path
**Impact:** No durable per-turn transcript for live calls today · **Fix:** new work, if ever needed (compliance, QA, dispute resolution)

It's fed by the legacy relay's inline event handling; the WebRTC path's transcript only exists client-side, in the DOM, for the duration of the browser tab. If server-side, durable, per-turn transcript logging is ever needed for the WebRTC path, that's new work, not something already wired up and merely unused.

### Issue 9 — `data/plans_config.json` may be dead
**Impact:** Unclear — unconfirmed whether anything reads it · **Fix:** confirm before deleting or assuming it affects pricing

It exists in `backend/data/` alongside `plans.csv` and `plan_availability.csv`, but `plan_matcher.py` only reads the two CSVs. Confirm whether this JSON file is leftover from an earlier pricing-config format, or whether something else in the codebase still reads it.

---

## 14. Testing

```bash
cd backend && python -m pytest tests/ -v
```

**No API key and no network access are needed to run the suite** — it only exercises the pure, deterministic functions (extraction, plan matching, the FSM, tool-call validation logic), never the live OpenAI connection itself. Tests that touch storage (`test_lead_recovery.py`, `test_usage_summary.py`, parts of `test_realtime_tools.py`) redirect `config.APP_DB_PATH` to a `tmp_path` fixture first, so nothing in the suite touches the real `quote_assistant.sqlite3` or `leads_fallback.jsonl` files, or makes any outbound network call.

| Test file | Covers |
|---|---|
| `test_state_machine.py` | The FSM's stage transitions end-to-end through a full happy-path call. |
| `test_extraction.py` | Speech/text → clean value parsing (email, phone, zip, name, choice-matching). |
| `test_plan_matcher.py` | CSV-backed plan lookup for known and empty combos. |
| `test_call_service.py` | E.164 phone normalization. |
| `test_lead_recovery.py` | The abandoned-lead sweeper's find/mark logic against a real (temp) SQLite DB. |
| `test_realtime_session_watchdog.py` | **Legacy path.** `RealtimeSession._watchdog()`/`_note_stage_for_call_ending()`, exercised directly with monkeypatched tiny timeouts — no real WebSocket needed. |
| `test_realtime_tools.py` | The largest and most important suite (425 lines) — stage validation, plan-id validation, phone digit accumulation, date/time regex, double-booking prevention. Added specifically because this boundary (model tool calls → real backend effects) previously had zero coverage despite being the most important guard rail in the whole S2S path. |
| `test_usage_summary.py` | Token/cost usage summary math, including the whisper-only-call edge case. |

If you add a new tool, a new FSM stage, or change validation logic anywhere in `realtime_tools.py` or `state_machine.py`, add a test in the matching file above — the existing suite is the actual safety net for the anti-hallucination guarantee described in [Section 3](#3-the-core-design-principle); it is not optional scaffolding.

---

## 15. Running & Deploying

### Local development

```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit — see §13 re: stale defaults in this file
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000` — FastAPI serves the frontend directly from the same origin (see `main.py`'s `StaticFiles` mount), so there's no separate frontend dev server or CORS setup needed for local work.

**Mobile testing** — mic access requires HTTPS on a real phone:

```bash
npx localtunnel --port 8000
```

### Deployment

- **`Dockerfile`** — `python:3.12-slim`, installs `backend/requirements.txt`, runs `uvicorn main:app --host 0.0.0.0 --port $PORT` from inside `backend/`.
- **`Procfile`** — same command, for a Heroku/Railway-style buildpack deployment instead of the Dockerfile.
- Either way, **the frontend is served by the same process** (`StaticFiles` mount in `main.py`) — there is nothing separate to deploy for the widget itself.
- Embedding on a client's actual site: a single `<script src=".../realtime-widget.js" data-key="pk_live_xxx"></script>` tag. The `data-key` attribute is only required if `REQUIRE_API_KEY=true` on the backend (off by default).
> [!WARNING]
> **Before running more than one backend process** (horizontal scaling, multiple dynos/containers): `SESSIONS` and `SESSION_LOCKS` in `main.py` are in-process Python dicts. A second process has a disjoint session table and will 404 on any session created by the first — this needs to move to a shared store (Redis is the natural fit, `session_store.py`'s docstring explicitly anticipates this) before scaling horizontally.

---

## 16. Glossary

| Term | Meaning |
|---|---|
| **FSM** | Finite State Machine — this project's term for `DialogueManager` in `state_machine.py`, the deterministic brain that owns every question/price/transition. |
| **Stage** | One step in the FSM (e.g. `full_name`, `tonnage`, `plan_choice`). Stored as `session.stage`. |
| **Slot** | One collected value, keyed by stage name, stored in `session.slots`. |
| **`say_next`** | The exact string a tool-call result hands back for the model to speak verbatim — the mechanism that prevents the model from composing its own reply. |
| **S2S** | Speech-to-Speech — OpenAI's Realtime API mode where audio goes in and audio comes out over one continuous connection, no separate STT/TTS round trip. |
| **Realtime API** | OpenAI's product this entire voice layer is built on (`wss://api.openai.com/v1/realtime` / `https://api.openai.com/v1/realtime/calls`). |
| **VAD** | Voice Activity Detection — deciding when a caller has started/stopped talking. Two modes used here: `semantic_vad` (judges from linguistic completeness) and `server_vad` (judges from a flat silence duration). |
| **TPM** | Tokens Per Minute — OpenAI's rate-limit dimension for the Realtime API; see §13, Issues 3 and 4, for this project's specific history with it. |
| **Ephemeral token / client secret** | A short-lived, browser-safe credential minted server-side (`/realtime-token`) so the browser can open its own connection to OpenAI without ever seeing the real `OPENAI_API_KEY`. |
| **Data channel (`dc`)** | The WebRTC data channel (`"oai-events"`) carrying JSON control events (transcripts, tool calls, turn signals) alongside the separate audio track. |
| **Relay / Gen 1** | The WebSocket architecture where this backend proxied every audio frame between browser and OpenAI. Legacy fallback only, at `/ws/realtime/{id}`. |
| **Lead** | The final structured record (`session.slots` + `available_plans` + outcome) delivered via `notify.py` once a call reaches `closing`. |
| **Partial lead** | A lead recovered from an abandoned (idle, never-finished) call by the background sweeper in `main.py`. |

---

## 17. FAQ

**Q: Why not let the model just talk freely instead of forcing everything through tools?**
Because pricing and plan names can never be allowed to be hallucinated. The model can only call `confirm_slot` (and a handful of other narrow tools); the code decides the next question and hands the model the exact sentence to say. See [Section 3](#3-the-core-design-principle).

**Q: I'm debugging a live call and looking at `realtime_session.py` — is that the right file?**
Almost certainly not, unless you're specifically working on the WebRTC-unavailable fallback path. The live frontend speaks WebRTC directly to OpenAI; the file that matters is `frontend/realtime-widget.js`. See [Section 4](#4-two-architecture-generations-important).

**Q: How do I change a price or which plans are offered for a given system size?**
Edit `backend/data/plans.csv` (price/features) or `backend/data/plan_availability.csv` (which plans apply to which category+tonnage+location combo). No code change, no restart needed — `plan_matcher.py` checks file mtimes on every read.

**Q: How do I add a new question to the flow?**
If it's a fixed-order question, add an entry to `dialogue/slots.py`'s `STAGES` dict and insert it into `ORDER` at the right position. If its options or availability depend on session data, add branching logic to `state_machine.py::_stage_meta()` and `::_advance()` instead, following the pattern of `plan_choice`/`plan_action`. Either way, add coverage in `tests/test_state_machine.py` and (if the model needs to be able to answer it via a tool call) update `realtime_tools.py::_confirm_slot_schema` and add a case to `tests/test_realtime_tools.py`.

**Q: How do I check what a test call actually cost, instead of estimating?**
`GET /api/session/{session_id}/usage` — see [Section 12](#usage--cost-tracking) for how to get the session ID from a live browser tab.

**Q: What was the hardest bug in this project's history?**
The response-queueing/rate-limit cluster described in [Section 13](#13-known-issues-drift--technical-debt), Issues 3 and 4 — six different triggers competing to start a spoken response, compounded by a real TPM rate-limit ceiling being hit mid-call, compounded further by a retry mechanism that (before being fixed) waited out OpenAI's full ~60-second reset window and raced against an independent 2.5-second watchdog, producing an uncaught crash. All three layers (queueing, trimming, and guarded retries/sends) had to be right together — fixing any one alone left a real failure mode live.

**Q: How do I stop background noise from being treated as speech?**
On the current WebRTC path, this is entirely OpenAI's server-side job (`semantic_vad` / `server_vad`, tuned via `REALTIME_VAD_*` settings — see §12). There is no client-side VAD/silence gate left on this path; that mechanism belonged to the legacy relay path only (`audio-worklets.js` / `realtime_session.py`), which used a client-side RMS threshold plus a server-side minimum-audio-length check before treating a "turn" as real speech.