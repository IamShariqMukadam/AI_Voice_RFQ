import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
from models import SessionState
from services import notify, session_store


def test_session_store_finds_and_marks_abandoned_lead(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DB_PATH", str(tmp_path / "sessions.sqlite3"))
    session_store.init_db()
    session = SessionState()
    session.stage = "tonnage"
    session.slots = {"full_name": "Shariq Mukadam", "phone": "7378850558"}

    session_store.save_progress(session)

    old = time.time() - 1000
    with session_store._connect() as conn:
        conn.execute(
            "UPDATE lead_snapshots SET updated_at = ? WHERE session_id = ?",
            (old, session.session_id),
        )

    leads = session_store.find_abandoned_leads(900)
    assert [lead["session_id"] for lead in leads] == [session.session_id]
    assert leads[0]["slots"]["phone"] == "7378850558"

    session_store.mark_abandoned_notified(session.session_id)
    assert session_store.find_abandoned_leads(900) == []


def test_submit_partial_lead_uses_fallback_file(tmp_path, monkeypatch):
    fallback = tmp_path / "leads.jsonl"
    monkeypatch.setattr(config, "WP_SUBMIT_ENDPOINT", "")
    monkeypatch.setattr(config, "SMTP_HOST", "")
    monkeypatch.setattr(config, "NOTIFY_EMAIL_TO", "")
    monkeypatch.setattr(config, "LEADS_FALLBACK_FILE", str(fallback))

    ok = notify.submit_partial_lead(
        "abc123",
        {"full_name": "Shariq Mukadam", "phone": "7378850558"},
        stage="tonnage",
    )

    assert ok is True
    payload = json.loads(fallback.read_text().strip())
    assert payload["session_id"] == "abc123"
    assert payload["status"] == "incomplete_abandoned"
