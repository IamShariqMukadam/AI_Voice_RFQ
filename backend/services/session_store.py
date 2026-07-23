"""
Small durable store for active sessions and partial leads.

This intentionally uses SQLite from the Python standard library so the
demo/pilot can save progress without adding infrastructure. In production,
the same API can be backed by Postgres/Redis with minimal changes to main.py.
"""
import json
import logging
import secrets
import sqlite3
import time
from pathlib import Path

import config
from models import SessionState

logger = logging.getLogger("session_store")


def _db_path() -> Path:
    return Path(config.APP_DB_PATH)


def _connect():
    path = _db_path()
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lead_snapshots (
                session_id TEXT PRIMARY KEY,
                stage TEXT NOT NULL,
                slots_json TEXT NOT NULL,
                available_plans_json TEXT NOT NULL,
                is_complete INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL
            )
            """
        )
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(lead_snapshots)").fetchall()
        }
        if "abandoned_notified_at" not in columns:
            conn.execute("ALTER TABLE lead_snapshots ADD COLUMN abandoned_notified_at REAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS realtime_transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                speaker TEXT NOT NULL,
                transcript TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS realtime_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                input_cached_tokens INTEGER NOT NULL DEFAULT 0,
                input_text_tokens INTEGER NOT NULL DEFAULT 0,
                input_audio_tokens INTEGER NOT NULL DEFAULT 0,
                output_text_tokens INTEGER NOT NULL DEFAULT 0,
                output_audio_tokens INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS realtime_whisper_audio (
                session_id TEXT PRIMARY KEY,
                bytes_sent INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS booked_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_date TEXT NOT NULL,
                call_time TEXT NOT NULL,
                session_id TEXT NOT NULL,
                appointment_type TEXT NOT NULL DEFAULT 'call',
                created_at REAL NOT NULL,
                UNIQUE(call_date, call_time)
            )
            """
        )
        booked_cols = {r[1] for r in conn.execute("PRAGMA table_info(booked_slots)").fetchall()}
        if "appointment_type" not in booked_cols:
            conn.execute("ALTER TABLE booked_slots ADD COLUMN appointment_type TEXT NOT NULL DEFAULT 'call'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                api_key TEXT PRIMARY KEY,
                client_name TEXT NOT NULL,
                plan_tier TEXT NOT NULL DEFAULT 'trial',
                monthly_quota INTEGER NOT NULL DEFAULT 500,
                requests_used INTEGER NOT NULL DEFAULT 0,
                period_start REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            )
            """
        )


def _session_payload(session: SessionState) -> dict:
    if hasattr(session, "model_dump"):
        return session.model_dump()
    return session.dict()


def save_session(session: SessionState) -> None:
    now = time.time()
    payload = _session_payload(session)
    payload["updated_at"] = now
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_id, state_json, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (session.session_id, json.dumps(payload), session.created_at, now),
        )


def load_session(session_id: str) -> SessionState | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT state_json FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if not row:
        return None
    try:
        return SessionState(**json.loads(row["state_json"]))
    except Exception as exc:
        logger.warning("[%s] failed to load saved session: %s", session_id, exc)
        return None


def save_lead_snapshot(session: SessionState, *, is_complete: bool = False) -> None:
    """Save whatever we know so far.

    A partial row is useful even if the customer never reaches final submit:
    sales can still recover a lead once name/phone/email or HVAC details
    exist. Empty just-started sessions are intentionally ignored.
    """
    if not session.slots:
        return
    now = time.time()
    complete_flag = 1 if is_complete else 0
    completed_at = now if is_complete else None
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO lead_snapshots (
                session_id, stage, slots_json, available_plans_json,
                is_complete, created_at, updated_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                stage = excluded.stage,
                slots_json = excluded.slots_json,
                available_plans_json = excluded.available_plans_json,
                is_complete = MAX(lead_snapshots.is_complete, excluded.is_complete),
                updated_at = excluded.updated_at,
                completed_at = COALESCE(lead_snapshots.completed_at, excluded.completed_at)
            """,
            (
                session.session_id,
                session.stage,
                json.dumps(session.slots),
                json.dumps(session.available_plans),
                complete_flag,
                session.created_at,
                now,
                completed_at,
            ),
        )


def save_progress(session: SessionState, *, is_complete: bool = False) -> None:
    try:
        save_session(session)
        save_lead_snapshot(session, is_complete=is_complete)
    except Exception as exc:
        logger.error("[%s] failed to save session progress: %s", session.session_id, exc)


def find_abandoned_leads(min_idle_seconds: int) -> list[dict]:
    """Return incomplete, contactable leads that have gone idle.

    A lead is considered contactable once it has at least a phone number
    or email. The notification marker keeps the sweeper from repeatedly
    sending the same abandoned lead.
    """
    cutoff = time.time() - min_idle_seconds
    rows = []
    with _connect() as conn:
        for row in conn.execute(
            """
            SELECT session_id, stage, slots_json, available_plans_json, updated_at
            FROM lead_snapshots
            WHERE is_complete = 0
              AND abandoned_notified_at IS NULL
              AND updated_at < ?
            ORDER BY updated_at ASC
            """,
            (cutoff,),
        ).fetchall():
            try:
                slots = json.loads(row["slots_json"] or "{}")
                available_plans = json.loads(row["available_plans_json"] or "[]")
            except json.JSONDecodeError:
                logger.warning("[%s] skipped corrupt abandoned lead snapshot", row["session_id"])
                continue
            if not (slots.get("phone") or slots.get("email")):
                continue
            rows.append(
                {
                    "session_id": row["session_id"],
                    "stage": row["stage"],
                    "slots": slots,
                    "available_plans": available_plans,
                    "updated_at": row["updated_at"],
                }
            )
    return rows


def log_transcript_turn(session_id: str, speaker: str, transcript: str) -> None:
    """S2S replacement for the old stt_debug/ audio-file logging: the
    Realtime API doesn't expose an intermediate audio artifact the way
    Whisper did, so this stores the auxiliary transcription events
    (conversation.item.input_audio_transcription.completed /
    response.audio_transcript.done) for audit/logging purposes only.

    IMPORTANT (kept from the migration notes on purpose): this transcript
    comes from an auxiliary transcription pass running alongside the core
    S2S model, not a guaranteed exact window into what the model actually
    "heard" or reasoned about. Treat it as a call log, not as ground truth
    the way the old stt_debug/ candidate_* files were for debugging a
    specific mis-hearing.
    """
    if not transcript:
        return
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO realtime_transcripts (session_id, speaker, transcript, created_at) VALUES (?, ?, ?, ?)",
                (session_id, speaker, transcript, time.time()),
            )
    except Exception:
        logger.exception("[%s] failed to log realtime transcript turn", session_id)


def get_transcript(session_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT speaker, transcript, created_at FROM realtime_transcripts WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    return [{"speaker": r["speaker"], "transcript": r["transcript"], "created_at": r["created_at"]} for r in rows]


def log_realtime_usage(session_id: str, usage: dict) -> None:
    """Records the usage block off each Realtime API response.done event
    - this is how you check what a test call actually cost instead of
    guessing from the per-minute estimate. input/output tokens are split
    into cached vs non-cached (cached is billed at a steep discount) and
    text vs audio (audio is the expensive part)."""
    input_details = usage.get("input_token_details", {}) or {}
    output_details = usage.get("output_token_details", {}) or {}
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO realtime_usage (
                    session_id, created_at,
                    input_tokens, output_tokens, total_tokens,
                    input_cached_tokens, input_text_tokens, input_audio_tokens,
                    output_text_tokens, output_audio_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, time.time(),
                    usage.get("input_tokens", 0), usage.get("output_tokens", 0), usage.get("total_tokens", 0),
                    input_details.get("cached_tokens", 0), input_details.get("text_tokens", 0), input_details.get("audio_tokens", 0),
                    output_details.get("text_tokens", 0), output_details.get("audio_tokens", 0),
                ),
            )
    except Exception:
        logger.exception("[%s] failed to log realtime usage", session_id)


def log_whisper_audio_bytes(session_id: str, bytes_sent: int) -> None:
    """Records how many bytes of raw 24kHz/16-bit/mono PCM were streamed
    for this call - that's exactly the audio OpenAI's side-channel
    `input_audio_transcription` (whisper-1) transcribes, and it's billed
    per minute ($0.017/min), separately from the realtime model's own
    audio tokens above. Called once at call end (see realtime_session.py
    run()'s cleanup) rather than per-chunk, to avoid a DB write every
    ~20-100ms during a live call."""
    if bytes_sent <= 0:
        return
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO realtime_whisper_audio (session_id, bytes_sent)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET bytes_sent = excluded.bytes_sent
                """,
                (session_id, bytes_sent),
            )
    except Exception:
        logger.exception("[%s] failed to log whisper audio bytes", session_id)


# Per-1M-token rates for gpt-realtime-mini as of this writing - used only
# to turn logged token counts into an approximate dollar figure. Confirm
# against developers.openai.com/api/docs/pricing before trusting this for
# real invoicing; it's an estimate, not a billing record.
_RATES = {
    "audio_in": 10.00, "audio_in_cached": 0.30, "audio_out": 20.00,
    "text_in": 0.60, "text_in_cached": 0.06, "text_out": 2.40,
    "whisper_per_min": 0.017,
}
_PCM16_MONO_24KHZ_BYTES_PER_SEC = 48000  # 24000 samples/sec * 2 bytes/sample


def get_usage_summary(session_id: str) -> dict:
    """Sums every logged response for this session into one token/cost
    breakdown - hit GET /api/session/{id}/usage after a test call."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM realtime_usage WHERE session_id = ?", (session_id,)
        ).fetchall()
        whisper_row = conn.execute(
            "SELECT bytes_sent FROM realtime_whisper_audio WHERE session_id = ?", (session_id,)
        ).fetchone()
    whisper_minutes = round((whisper_row["bytes_sent"] if whisper_row else 0) / _PCM16_MONO_24KHZ_BYTES_PER_SEC / 60, 4)
    whisper_cost = whisper_minutes * _RATES["whisper_per_min"]

    if not rows:
        if whisper_minutes == 0:
            return {"session_id": session_id, "responses": 0, "note": "no usage logged yet for this session"}
        return {
            "session_id": session_id, "responses": 0,
            "whisper_transcription_minutes": whisper_minutes,
            "estimated_cost_usd": round(whisper_cost, 5),
            "note": "no model responses logged yet, but caller audio was streamed (whisper cost only)",
        }

    totals = {k: sum(r[k] for r in rows) for k in (
        "input_tokens", "output_tokens", "total_tokens",
        "input_cached_tokens", "input_text_tokens", "input_audio_tokens",
        "output_text_tokens", "output_audio_tokens",
    )}
    audio_in_uncached = max(totals["input_audio_tokens"] - totals["input_cached_tokens"], 0)
    text_in_uncached = max(totals["input_text_tokens"] - totals["input_cached_tokens"], 0)
    est_cost = (
        audio_in_uncached / 1e6 * _RATES["audio_in"]
        + totals["output_audio_tokens"] / 1e6 * _RATES["audio_out"]
        + text_in_uncached / 1e6 * _RATES["text_in"]
        + totals["output_text_tokens"] / 1e6 * _RATES["text_out"]
        + whisper_cost
    )
    return {
        "session_id": session_id,
        "responses": len(rows),
        "tokens": totals,
        "whisper_transcription_minutes": whisper_minutes,
        "estimated_cost_usd": round(est_cost, 5),
        "note": "estimate from logged token counts + whisper minutes, not a real invoice - cross-check against your OpenAI dashboard",
    }


def book_call_slot(call_date: str, call_time: str, session_id: str, appointment_type: str = "call") -> bool:
    """Atomic via the UNIQUE(call_date, call_time) constraint - returns
    False (not True + a separate check) if someone already grabbed that
    exact slot, including under concurrent requests, since SQLite
    enforces the constraint at insert time rather than us doing a
    check-then-insert race. Calls and visits share the same slot table
    on purpose - the team can't be in two places at once either way."""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO booked_slots (call_date, call_time, session_id, appointment_type, created_at) VALUES (?, ?, ?, ?, ?)",
                (call_date, call_time, session_id, appointment_type, time.time()),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def is_slot_booked(call_date: str, call_time: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM booked_slots WHERE call_date = ? AND call_time = ?", (call_date, call_time)
        ).fetchone()
    return row is not None


def get_booked_slots_in_range(start_date: str, end_date: str) -> dict:
    """call_date -> [call_time, ...] for every booked slot in [start_date,
    end_date] (inclusive YYYY-MM-DD). Powers the schedule_appointment
    calendar widget's availability view - one query covers the whole
    bookable window (see config.SCHEDULE_MAX_DAYS_AHEAD) since there are
    realistically only a handful of actual bookings in it, not one row
    per possible slot, so there's no need for a per-day round trip."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT call_date, call_time FROM booked_slots WHERE call_date BETWEEN ? AND ? ORDER BY call_date, call_time",
            (start_date, end_date),
        ).fetchall()
    out: dict = {}
    for row in rows:
        out.setdefault(row["call_date"], []).append(row["call_time"])
    return out


# --------------------------------------------------------------------
# API keys - lightweight self-hosted key/quota tracking for offering
# this as a subscription API rather than handing over the source.
# No Stripe wiring here (that needs your live Stripe account/webhook
# secret to test) - this is the quota-enforcement half; billing can
# call issue_api_key()/set_quota() from a webhook handler later.
# --------------------------------------------------------------------

def issue_api_key(client_name: str, plan_tier: str = "trial", monthly_quota: int = 500) -> str:
    key = "pk_live_" + secrets.token_hex(20)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO api_keys (api_key, client_name, plan_tier, monthly_quota, requests_used, period_start, active, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?, 1, ?)",
            (key, client_name, plan_tier, monthly_quota, time.time(), time.time()),
        )
    return key


def check_api_key(api_key: str) -> dict:
    """Validates a key (exists, active, under quota) WITHOUT consuming a
    unit of usage. Use this at points where a session might never turn
    into a real, billable call (session creation, page-refresh resume) -
    see check_and_increment_api_key for the point that actually charges."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM api_keys WHERE api_key = ?", (api_key,)).fetchone()
        if not row:
            return {"ok": False, "reason": "invalid API key"}
        if not row["active"]:
            return {"ok": False, "reason": "API key disabled"}
        period_start = row["period_start"]
        used = 0 if time.time() - period_start > 30 * 86400 else row["requests_used"]
        if used >= row["monthly_quota"]:
            return {"ok": False, "reason": "monthly quota exceeded"}
    return {"ok": True, "reason": None}


def check_and_increment_api_key(api_key: str) -> dict:
    """Returns {'ok': bool, 'reason': str|None}. Resets the monthly
    counter automatically once 30 days have passed since period_start -
    no cron job needed for that part. This is the ONE place usage is
    actually charged - callers must only call it once per real, billable
    call (see main.py's _consume_api_key_once, which guards this with a
    per-session idempotency flag so reconnects/refreshes don't re-charge)."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM api_keys WHERE api_key = ?", (api_key,)).fetchone()
        if not row:
            return {"ok": False, "reason": "invalid API key"}
        if not row["active"]:
            return {"ok": False, "reason": "API key disabled"}
        period_start = row["period_start"]
        if time.time() - period_start > 30 * 86400:
            conn.execute(
                "UPDATE api_keys SET requests_used = 0, period_start = ? WHERE api_key = ?",
                (time.time(), api_key),
            )
            used = 0
        else:
            used = row["requests_used"]
        if used >= row["monthly_quota"]:
            return {"ok": False, "reason": "monthly quota exceeded"}
        conn.execute("UPDATE api_keys SET requests_used = requests_used + 1 WHERE api_key = ?", (api_key,))
    return {"ok": True, "reason": None}


def mark_abandoned_notified(session_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE lead_snapshots SET abandoned_notified_at = ? WHERE session_id = ?",
            (time.time(), session_id),
        )