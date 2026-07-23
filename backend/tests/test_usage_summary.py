import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
from services import session_store


def _redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DB_PATH", str(tmp_path / "usage_test.sqlite3"))
    session_store.init_db()


def test_usage_summary_includes_whisper_only_call(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    # 30 seconds of 24kHz/16-bit/mono audio = 30 * 48000 bytes
    session_store.log_whisper_audio_bytes("s1", 30 * 48000)

    summary = session_store.get_usage_summary("s1")
    assert summary["whisper_transcription_minutes"] == 0.5
    assert summary["estimated_cost_usd"] == round(0.5 * 0.017, 5)


def test_usage_summary_adds_whisper_cost_on_top_of_token_cost(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    session_store.log_realtime_usage("s2", {
        "input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500,
        "input_token_details": {"cached_tokens": 0, "text_tokens": 200, "audio_tokens": 800},
        "output_token_details": {"text_tokens": 100, "audio_tokens": 400},
    })
    session_store.log_whisper_audio_bytes("s2", 60 * 48000)  # 1 minute

    summary = session_store.get_usage_summary("s2")
    token_cost = (800 / 1e6 * 10.00) + (400 / 1e6 * 20.00) + (200 / 1e6 * 0.60) + (100 / 1e6 * 2.40)
    expected = round(token_cost + 1.0 * 0.017, 5)
    assert summary["estimated_cost_usd"] == expected
    assert summary["whisper_transcription_minutes"] == 1.0


def test_usage_summary_zero_bytes_does_not_create_row(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    session_store.log_whisper_audio_bytes("s3", 0)  # no-op, guarded in the function
    summary = session_store.get_usage_summary("s3")
    assert summary["responses"] == 0
    assert summary["note"] == "no usage logged yet for this session"
