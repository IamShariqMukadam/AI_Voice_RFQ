import asyncio
import logging

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from dialogue.state_machine import DialogueManager
from models import SessionState
from services import notify, realtime_tools, session_store
from services.realtime_session import RealtimeSession, GREETING_INSTRUCTIONS, build_session_dict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("main")

app = FastAPI(title="AC / Heating Instant Quote Voice Assistant - S2S (OpenAI Realtime)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory session store. Fine for a single-instance demo/pilot; move to
# Redis before running more than one backend process (see README).
SESSIONS: dict[str, SessionState] = {}

# One lock per live call, same purpose as RealtimeSession._stage_lock had
# on the WebSocket-relay path: a model-driven function call (arriving via
# /tool-call from the browser's WebRTC data channel) and a button tap
# (arriving via the same endpoint) can both race to read-mutate-send
# session.stage. Serializing per-session here prevents the exact
# interleaving bug that lock's docstring (see realtime_session.py)
# describes - now enforced across separate HTTP requests instead of
# separate asyncio tasks.
SESSION_LOCKS: dict[str, asyncio.Lock] = {}


def _get_session_lock(session_id: str) -> asyncio.Lock:
    lock = SESSION_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        SESSION_LOCKS[session_id] = lock
    return lock


@app.on_event("startup")
async def startup():
    session_store.init_db()
    asyncio.create_task(_abandoned_lead_sweeper())


async def _abandoned_lead_sweeper():
    while True:
        try:
            abandoned = session_store.find_abandoned_leads(config.ABANDONED_LEAD_IDLE_SECONDS)
            for lead in abandoned:
                delivered = notify.submit_partial_lead(
                    lead["session_id"],
                    lead["slots"],
                    available_plans=lead.get("available_plans"),
                    stage=lead.get("stage", ""),
                )
                if delivered:
                    session_store.mark_abandoned_notified(lead["session_id"])
                    logger.info("[%s] abandoned lead recovered", lead["session_id"])
        except Exception:
            logger.exception("abandoned lead sweeper failed")
        await asyncio.sleep(config.ABANDONED_LEAD_SWEEP_SECONDS)


def _get_session(session_id: str) -> SessionState:
    session = SESSIONS.get(session_id)
    if not session:
        session = session_store.load_session(session_id)
        if session:
            SESSIONS[session.session_id] = session
    if not session:
        raise HTTPException(404, "Session not found or expired. Start a new one.")
    return session


# ---------------------------------------------------------------------------
# API-as-a-service gating. Off by default (REQUIRE_API_KEY unset) so nothing
# changes for the current single-client deployment - flip it on once you're
# actually issuing keys to separate customers. WebSocket connections can't
# set custom headers, so the key travels as a query param there instead.
#
# Quota is charged exactly ONCE per real phone call, at the point the
# realtime call actually connects (see _consume_api_key_once below) - not
# at session creation and not on every page-refresh resume. Previously
# every one of those three touchpoints called the same consuming check,
# so a single call could burn 2-3 units of a client's monthly_quota.
# ---------------------------------------------------------------------------

def _check_api_key(api_key: str | None):
    """Validates the key without charging usage - safe to call any
    number of times (session creation, page-refresh resume) since a
    session isn't necessarily going to turn into a real, billable call."""
    if not config.REQUIRE_API_KEY:
        return
    if not api_key:
        raise HTTPException(401, "Missing X-API-Key header.")
    result = session_store.check_api_key(api_key)
    if not result["ok"]:
        raise HTTPException(403, result["reason"])


def _consume_api_key_once(session: SessionState, api_key: str | None):
    """Charges exactly one unit of quota for one real phone call, no
    matter how many times the browser reconnects the websocket for the
    same session_id (dropped connection, page refresh mid-call, etc).
    Idempotent per session via session.slots['_quota_charged'], same
    pattern as DialogueManager._submit_lead_once for lead submission."""
    if not config.REQUIRE_API_KEY:
        return
    if session.slots.get("_quota_charged"):
        return
    if not api_key:
        raise HTTPException(401, "Missing API key.")
    result = session_store.check_and_increment_api_key(api_key)
    if not result["ok"]:
        raise HTTPException(403, result["reason"])
    session.slots["_quota_charged"] = True


class IssueKeyRequest(BaseModel):
    client_name: str
    plan_tier: str = "trial"
    monthly_quota: int = 500


@app.post("/api/admin/keys")
async def issue_key(body: IssueKeyRequest, x_admin_secret: str = Header(default="")):
    """Issues a new client API key. Protected by a single master secret
    (ADMIN_API_SECRET in .env) - this is YOUR endpoint to call when
    onboarding a new customer, not something you expose to them.
    No Stripe wiring here (needs your live Stripe account/webhook secret
    to test) - call this from your own billing webhook handler once a
    subscription is created, or by hand for now."""
    if not config.ADMIN_API_SECRET or x_admin_secret != config.ADMIN_API_SECRET:
        raise HTTPException(403, "Invalid or missing admin secret.")
    key = session_store.issue_api_key(body.client_name, body.plan_tier, body.monthly_quota)
    return {"api_key": key, "client_name": body.client_name, "plan_tier": body.plan_tier, "monthly_quota": body.monthly_quota}


@app.post("/api/session/start")
async def start_session(x_api_key: str = Header(default="")):
    """Creates the session record. No TTS/audio here on purpose - the
    greeting is spoken live by the model once /ws/realtime connects,
    same as every other turn. This just gives the frontend a session_id
    to open that socket with."""
    _check_api_key(x_api_key)
    session = SessionState()
    SESSIONS[session.session_id] = session
    display_text, _ = DialogueManager(session).greeting()
    session_store.save_progress(session, is_complete=False)
    return {
        "session_id": session.session_id,
        "stage": session.stage,
        "slots": session.slots,
        "assistant_text": display_text,
        "ui": DialogueManager(session).ui_for_stage(),
    }


@app.get("/api/session/{session_id}")
async def resume_session(session_id: str, x_api_key: str = Header(default="")):
    _check_api_key(x_api_key)
    session = _get_session(session_id)
    display_text, _ = DialogueManager(session)._entry_text(session.stage)
    return {
        "session_id": session.session_id,
        "stage": session.stage,
        "slots": session.slots,
        "assistant_text": display_text,
        "ui": DialogueManager(session).ui_for_stage(),
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "openai_configured": bool(config.OPENAI_API_KEY)}


@app.get("/api/session/{session_id}/usage")
async def session_usage(session_id: str):
    """Per-call token/cost tracking - see services/realtime_session.py's
    response.done handling, which is what populates this table live
    during the call. Use this after a test call instead of guessing."""
    _get_session(session_id)  # 404s if unknown
    return session_store.get_usage_summary(session_id)


@app.post("/api/session/{session_id}/log-usage")
async def log_usage(session_id: str, usage: dict):
    """Receives the usage block off response.done from the browser's own
    WebRTC data channel (see realtime-widget.js) - the backend has no
    other way to see it now that audio/events go straight browser<->OpenAI."""
    _get_session(session_id)  # 404s if unknown
    session_store.log_realtime_usage(session_id, usage)
    return {"ok": True}


@app.post("/api/session/{session_id}/realtime-token")
async def realtime_token(session_id: str, x_api_key: str = Header(default="")):
    """Mints a short-lived client token for the Realtime API. Never expose
    OPENAI_API_KEY itself to the frontend. Used by webrtc-widget.js to
    open its RTCPeerConnection straight to OpenAI - audio flows browser
    <-> OpenAI directly from here on, never through this backend (see
    realtime-session-config and /tool-call below for how business logic
    still stays server-side without an audio relay). The old /ws/realtime
    WebSocket relay further down is kept only as a fallback.
    Gated the same way as every other endpoint (_check_api_key) - this
    was previously unauthenticated, so anyone with a valid session_id
    could mint a real OpenAI ephemeral token even with REQUIRE_API_KEY
    on. This only validates the key; it does not charge quota - /tool-call
    is the sole charge point on this path (mirrors _consume_api_key_once's
    placement on the old path)."""
    _check_api_key(x_api_key)
    _get_session(session_id)
    if not config.OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY not configured on the server.")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                # BUG FIX: /v1/realtime/sessions is the retired beta
                # endpoint (404 on current accounts) - GA renamed this to
                # /v1/realtime/client_secrets, and the body must be
                # wrapped under "session" with voice under
                # audio.output.voice instead of a flat "voice" key.
                # Response shape also changed: token is top-level "value"
                # now, not "client_secret.value" (frontend already
                # handles both via `tokenData.client_secret?.value ||
                # tokenData.value`, so no frontend change needed here).
                "https://api.openai.com/v1/realtime/client_secrets",
                headers={
                    "Authorization": f"Bearer {config.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "session": {
                        "type": "realtime",
                        "model": config.REALTIME_MODEL,
                        "audio": {"output": {"voice": config.REALTIME_VOICE}},
                    }
                },
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError:
        logger.exception("[%s] failed to mint realtime ephemeral token", session_id)
        raise HTTPException(502, "Could not reach the voice engine provider.")


@app.get("/api/session/{session_id}/realtime-session-config")
async def realtime_session_config(session_id: str, x_api_key: str = Header(default="")):
    """WebRTC frontend path: after the browser opens its RTCPeerConnection
    straight to OpenAI (see /realtime-token above for the ephemeral key)
    and its data channel comes up, it sends this "session" object back to
    OpenAI as one session.update over that data channel - configuring
    instructions/tools/turn_detection identically to what the legacy
    server-relayed path sends via _send_initial_session_update. Also
    hands back the greeting line so the very first response.create the
    browser fires matches the old behavior exactly. No audio ever
    transits this backend on this path - only this one config blob and,
    per turn, /tool-call below."""
    _check_api_key(x_api_key)
    session = _get_session(session_id)
    return {
        "session": build_session_dict(session),
        "greeting_instructions": GREETING_INSTRUCTIONS,
    }


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict = {}


@app.post("/api/session/{session_id}/tool-call")
async def realtime_tool_call(session_id: str, body: ToolCallRequest, x_api_key: str = Header(default="")):
    """WebRTC frontend path: runs ONE tool call's business logic
    (confirm_slot, go_back_or_edit, schedule_appointment, etc) and
    returns the result plus updated stage/slots/ui - the REST-call
    equivalent of what _handle_tool_call/_handle_client_control used to
    do inline over the WebSocket relay. Covers both:
      - model-driven function calls, forwarded verbatim by the browser
        from a response.function_call_arguments.done data-channel event
      - UI-driven actions (option-card tap, Back, Change, schedule
        widget), which the browser translates into the same
        {name, arguments} shape before calling this (see
        webrtc-widget.js's callTool()) - e.g. a tapped option becomes
        name="confirm_slot", arguments={field: currentStage, value}.
    Charges quota here (not at token-mint) since this is the first point
    in the WebRTC flow that corresponds to a real, in-progress call -
    mirrors _consume_api_key_once's placement on the old path."""
    _check_api_key(x_api_key)
    session = _get_session(session_id)
    _consume_api_key_once(session, x_api_key)
    async with _get_session_lock(session_id):
        prev_stage = session.stage
        result = await asyncio.to_thread(realtime_tools.call_tool, session, body.name, body.arguments)
        logger.info(
            "[%s] tool_call %s(%s) at stage=%s -> ok=%s%s",
            session_id, body.name, body.arguments, prev_stage, result.get("ok"),
            "" if result.get("ok") else f" error={result.get('error')!r}",
        )
        # session_store.save_progress(session, is_complete=session.stage == "closing")
        say_next = result.get("say_next")
        if not result.get("ok") and say_next is None:
            # BUG FIX ("stuck after arrange a call" - reported 5+ times): a
            # rejected tool call (e.g. the model tries to confirm a field
            # ahead of the caller's real current stage - see
            # handle_confirm_slot's field-mismatch guard) used to return
            # with no say_next at all. The frontend (handleFunctionCall)
            # then did nothing further on rejection - no forced
            # response.create - so the assistant just went silent until
            # the NEXT voice-detected turn, while the model's own belief
            # about what it just confirmed had already drifted from the
            # real FSM stage (it had, e.g., already spoken an
            # acknowledgment for a field the tool call never actually
            # saved). Re-grounding both sides in the ACTUAL current
            # question here, on every rejection, means the caller always
            # hears something instead of dead air.
            _, say_next = DialogueManager(session)._entry_text(session.stage)
        response = {
            "ok": result.get("ok", False),
            "error": result.get("error"),
            "say_next": say_next,
            "stage": session.stage,
            "slots": session.slots,
            "ui": DialogueManager(session).ui_for_stage(),
        }
        # Only send an updated tools list when the stage actually moved -
        # same "instructions block stays cached, only tools changes"
        # reasoning as _refresh_tools_for_stage on the legacy path. The
        # browser resends this via session.update on its data channel
        # whenever this key is present.
        if session.stage != prev_stage:
            response["tools"] = realtime_tools.tools_for_stage(session)
        return response


@app.websocket("/ws/realtime/{session_id}")
async def realtime_ws(websocket: WebSocket, session_id: str, api_key: str = Query(default="")):
    """LEGACY PATH. Continuous audio relay for one call: browser mic ->
    here -> OpenAI Realtime API -> here -> browser speaker. Superseded by
    the WebRTC path (realtime-token + realtime-session-config +
    /tool-call above, used by webrtc-widget.js) - this double network hop
    (OpenAI -> this process -> browser, over WebSocket/TCP instead of
    WebRTC) is what was producing the choppy/laggy audio; WebRTC carries
    audio browser<->OpenAI directly with no relay in between. Kept only
    as a fallback if WebRTC is ever unavailable (e.g. a restrictive
    network blocking it) - not used by the current frontend.
    api_key travels as a query param (not a header) because the browser
    WebSocket API can't set custom headers on the upgrade request."""
    await websocket.accept()
    try:
        session = _get_session(session_id)
        _consume_api_key_once(session, api_key)
    except HTTPException as exc:
        await websocket.send_json({"type": "error", "message": exc.detail})
        await websocket.close()
        return

    rt_session = RealtimeSession(session, websocket)
    try:
        await rt_session.run()
    except WebSocketDisconnect:
        logger.info("[%s] realtime websocket disconnected", session_id)
    finally:
        session_store.save_progress(session, is_complete=session.stage == "closing")


app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")