"""
Tests for RealtimeSession's cost-safety watchdog: the mechanism that
hangs up the OpenAI Realtime connection instead of leaving it billing
per-minute forever. Exercises _watchdog()/_note_stage_for_call_ending()
directly with tiny monkeypatched timeouts - no real websocket needed,
since these methods only read config + internal timers.
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import config
from services.realtime_session import RealtimeSession


def _session(stage="tonnage"):
    return types.SimpleNamespace(stage=stage, session_id="test123")


def test_watchdog_ends_call_after_closing_grace_period(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_CALL_END_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(config, "REALTIME_IDLE_TIMEOUT_SECONDS", 999)
    monkeypatch.setattr(config, "REALTIME_MAX_CALL_SECONDS", 999)
    rt = RealtimeSession(_session(stage="closing"), client_ws=None)
    rt._note_stage_for_call_ending()

    asyncio.run(asyncio.wait_for(rt._watchdog(), timeout=2))
    assert rt._call_end_reason == "completed"


def test_watchdog_ends_call_on_idle_timeout(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_CALL_END_GRACE_SECONDS", 999)
    monkeypatch.setattr(config, "REALTIME_IDLE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(config, "REALTIME_MAX_CALL_SECONDS", 999)
    rt = RealtimeSession(_session(stage="tonnage"), client_ws=None)

    asyncio.run(asyncio.wait_for(rt._watchdog(), timeout=2))
    assert rt._call_end_reason == "idle"


def test_watchdog_ends_call_on_max_duration(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_CALL_END_GRACE_SECONDS", 999)
    monkeypatch.setattr(config, "REALTIME_IDLE_TIMEOUT_SECONDS", 999)
    monkeypatch.setattr(config, "REALTIME_MAX_CALL_SECONDS", 0.05)
    rt = RealtimeSession(_session(stage="tonnage"), client_ws=None)

    asyncio.run(asyncio.wait_for(rt._watchdog(), timeout=2))
    assert rt._call_end_reason == "max_duration"


def test_watchdog_does_not_end_call_while_active_and_not_closing(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_CALL_END_GRACE_SECONDS", 999)
    monkeypatch.setattr(config, "REALTIME_IDLE_TIMEOUT_SECONDS", 999)
    monkeypatch.setattr(config, "REALTIME_MAX_CALL_SECONDS", 999)
    rt = RealtimeSession(_session(stage="tonnage"), client_ws=None)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(rt._watchdog(), timeout=0.3))


def test_note_stage_for_call_ending_only_flips_once():
    rt = RealtimeSession(_session(stage="tonnage"), client_ws=None)
    rt._note_stage_for_call_ending()
    assert rt._call_ending is False  # not on "closing" yet

    rt.session.stage = "closing"
    rt._note_stage_for_call_ending()
    first_ts = rt._call_ending_at
    assert rt._call_ending is True
    assert first_ts is not None

    rt._note_stage_for_call_ending()  # called again, e.g. a second tool call
    assert rt._call_ending_at == first_ts  # grace period start doesn't reset
