"""
Tests for realtime_tools.py - the boundary between the Realtime model's
tool calls and the real backend (stage validation, plan-id validation,
phone digit accumulation, date/time regex, double-booking). This file
previously had zero coverage despite being the most important guard
rail in the S2S path (see demo review notes).

Every test that can reach a lead submission or slot booking redirects
storage to a tmp path first (same pattern as test_lead_recovery.py) so
nothing here touches the real quote_assistant.sqlite3 / leads_fallback
files or makes a network call.
"""
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import config
from models import SessionState
from dialogue.state_machine import DialogueManager
from services import realtime_tools, session_store

# handle_schedule_appointment rejects anything in the past or more than
# SCHEDULE_MAX_DAYS_AHEAD out (see realtime_tools.py), computed against
# "today" in config.CALENDAR_TIMEZONE. A hardcoded literal date ages out
# of that window every ~30 days, so compute a date that's always valid
# relative to whenever the suite actually runs.
_FUTURE_DATE = (
    datetime.now(ZoneInfo(config.CALENDAR_TIMEZONE)).date() + timedelta(days=5)
).isoformat()


def _fresh_session(stage="full_name", **slots):
    s = SessionState()
    s.stage = stage
    s.slots.update(slots)
    return s


def _to_stage(stage):
    """Drives a fresh session through the real DialogueManager (same
    path the cascaded pipeline uses) up to `stage`, so tests exercise
    realistic slot/available_plans state rather than hand-faked ones."""
    s = SessionState()
    d = DialogueManager(s)
    d.greeting()
    d.handle_manual("full_name", "Jane Doe")
    d.handle_manual("phone", "7378850558")
    d.handle_manual("email", "jane@example.com")
    d.handle_manual("street", "123 Main St")
    d.handle_manual("city", "Pune")
    d.handle_manual("zip", "411048")
    if stage in ("category", "full_name", "phone", "email", "street", "city", "zip"):
        return s
    d.handle_manual("category", "cooling_electric_heat")
    if stage == "tonnage":
        return s
    d.handle_manual("tonnage", "2_ton")
    if stage == "location":
        return s
    d.handle_manual("location", "attic_horizontal")  # has 3 plans: cooling_better, heating_better, cooling_best
    return s


@pytest.fixture
def redirect_storage(tmp_path, monkeypatch):
    """Points sqlite + lead fallback file at tmp_path for the duration
    of one test, and disables outbound delivery paths."""
    monkeypatch.setattr(config, "APP_DB_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setattr(config, "WP_SUBMIT_ENDPOINT", "")
    monkeypatch.setattr(config, "SMTP_HOST", "")
    monkeypatch.setattr(config, "LEADS_FALLBACK_FILE", str(tmp_path / "leads.jsonl"))
    session_store.init_db()
    yield


# ---- confirm_slot: stage guard --------------------------------------------

def test_confirm_slot_rejects_field_that_is_not_current_stage():
    s = _fresh_session(stage="full_name")
    result = realtime_tools.handle_confirm_slot(s, "phone", "1234567890")
    assert result["ok"] is False
    assert "full_name" in result["error"]


def test_confirm_slot_accepts_current_stage_and_advances():
    s = _fresh_session(stage="full_name")
    result = realtime_tools.handle_confirm_slot(s, "full_name", "Jane Doe")
    assert result["ok"] is True
    assert result["stage"] == "phone"
    assert s.slots["full_name"] == "Jane Doe"


def test_confirm_slot_missing_field_is_rejected():
    s = _fresh_session(stage="full_name")
    result = realtime_tools.handle_confirm_slot(s, "", "whatever")
    assert result["ok"] is False


# ---- confirm_slot: choice validation ---------------------------------------

def test_confirm_slot_rejects_invalid_choice_value():
    s = _to_stage("category")
    result = realtime_tools.handle_confirm_slot(s, "category", "not_a_real_option")
    assert result["ok"] is False
    assert "valid option" in result["error"].lower()
    assert s.stage == "category"  # unchanged


def test_confirm_slot_accepts_valid_choice_value():
    s = _to_stage("category")
    result = realtime_tools.handle_confirm_slot(s, "category", "cooling_electric_heat")
    assert result["ok"] is True
    assert result["stage"] == "tonnage"


# ---- confirm_slot: plan_choice (dynamic options + multi-select) -----------

def test_confirm_slot_plan_choice_rejects_unknown_plan_id():
    s = _to_stage("location")
    d = DialogueManager(s)
    d.handle_manual("location", "attic_horizontal")
    assert s.stage == "plan_choice"
    result = realtime_tools.handle_confirm_slot(s, "plan_choice", "not_a_real_plan")
    assert result["ok"] is False
    assert "not a valid plan id" in result["error"]


def test_confirm_slot_plan_choice_accepts_comma_separated_valid_ids():
    s = _to_stage("location")
    DialogueManager(s).handle_manual("location", "attic_horizontal")
    assert s.stage == "plan_choice"
    result = realtime_tools.handle_confirm_slot(s, "plan_choice", "cooling_better,heating_better")
    assert result["ok"] is True
    assert s.slots["plan_choice"] == ["cooling_better", "heating_better"]


def test_confirm_slot_plan_choice_empty_value_rejected():
    s = _to_stage("location")
    result = realtime_tools.handle_confirm_slot(s, "plan_choice", "")
    assert result["ok"] is False


# ---- confirm_slot: phone digit accumulation --------------------------------

def test_confirm_slot_phone_full_number_in_one_call():
    s = _fresh_session(stage="phone")
    result = realtime_tools.handle_confirm_slot(s, "phone", "9876543210")
    assert result["ok"] is True
    assert s.slots["phone"] == "9876543210"
    assert s.stage == "email"


def test_confirm_slot_phone_accumulates_across_two_calls():
    s = _fresh_session(stage="phone")
    first = realtime_tools.handle_confirm_slot(s, "phone", "123456123")  # 9 digits
    assert first["ok"] is True  # tool call itself succeeds; FSM stays on phone asking for more
    assert s.stage == "phone"
    assert "phone" not in s.slots
    second = realtime_tools.handle_confirm_slot(s, "phone", "0")
    assert second["ok"] is True
    assert s.slots["phone"] == "1234561230"
    assert s.stage == "email"


def test_confirm_slot_phone_rejects_value_with_no_digits():
    s = _fresh_session(stage="phone")
    result = realtime_tools.handle_confirm_slot(s, "phone", "no digits here")
    assert result["ok"] is False


# ---- go_back_or_edit --------------------------------------------------------

def test_go_back_or_edit_repeat_does_not_change_stage():
    s = _to_stage("tonnage")
    result = realtime_tools.handle_go_back_or_edit(s, "repeat")
    assert result["ok"] is True
    assert result["stage"] == "tonnage"


def test_go_back_or_edit_go_back_steps_to_previous_stage():
    s = _to_stage("tonnage")
    result = realtime_tools.handle_go_back_or_edit(s, "go_back")
    assert result["ok"] is True
    assert result["stage"] == "category"


def test_go_back_or_edit_start_over_wipes_progress():
    s = _to_stage("tonnage")
    result = realtime_tools.handle_go_back_or_edit(s, "start_over")
    assert result["ok"] is True
    assert result["stage"] == "full_name"
    assert s.slots == {}


def test_go_back_or_edit_edit_field_requires_field_arg():
    s = _to_stage("tonnage")
    result = realtime_tools.handle_go_back_or_edit(s, "edit_field", field=None)
    assert result["ok"] is False


def test_go_back_or_edit_edit_field_rejects_unanswered_field():
    s = _to_stage("tonnage")
    result = realtime_tools.handle_go_back_or_edit(s, "edit_field", field="plan_choice")
    assert result["ok"] is False
    assert "hasn't been answered" in result["error"]


def test_go_back_or_edit_edit_field_jumps_to_answered_field():
    s = _to_stage("tonnage")
    result = realtime_tools.handle_go_back_or_edit(s, "edit_field", field="email")
    assert result["ok"] is True
    assert result["stage"] == "email"


def test_go_back_or_edit_unknown_action_is_rejected():
    s = _to_stage("tonnage")
    result = realtime_tools.handle_go_back_or_edit(s, "fly_to_the_moon")
    assert result["ok"] is False


# ---- schedule_appointment: stage guard + regex validation ------------------

def test_schedule_appointment_rejects_wrong_stage():
    s = _fresh_session(stage="tonnage")
    result = realtime_tools.handle_schedule_appointment(s, _FUTURE_DATE, "14:30")
    assert result["ok"] is False
    assert "tonnage" in result["error"]


def test_schedule_appointment_rejects_bad_date_format():
    s = _fresh_session(stage="schedule_appointment")
    result = realtime_tools.handle_schedule_appointment(s, "07/12/2026", "14:30")
    assert result["ok"] is False
    assert "YYYY-MM-DD" in result["error"]


def test_schedule_appointment_rejects_bad_time_format():
    s = _fresh_session(stage="schedule_appointment")
    result = realtime_tools.handle_schedule_appointment(s, _FUTURE_DATE, "2:30 PM")
    assert result["ok"] is False
    assert "HH:MM" in result["error"]


def test_schedule_appointment_rejects_hour_out_of_range():
    s = _fresh_session(stage="schedule_appointment")
    result = realtime_tools.handle_schedule_appointment(s, _FUTURE_DATE, "25:00")
    assert result["ok"] is False


def test_schedule_appointment_books_slot_and_reaches_closing(redirect_storage):
    s = _fresh_session(stage="schedule_appointment", full_name="Jane Doe")
    result = realtime_tools.handle_schedule_appointment(s, _FUTURE_DATE, "14:30")
    assert result["ok"] is True
    assert result["stage"] == "closing"
    assert s.slots["appointment_date"] == _FUTURE_DATE
    assert s.slots["appointment_time"] == "14:30"
    assert session_store.is_slot_booked(_FUTURE_DATE, "14:30") is True


def test_schedule_appointment_rejects_double_booking(redirect_storage):
    s1 = _fresh_session(stage="schedule_appointment", full_name="Jane Doe")
    first = realtime_tools.handle_schedule_appointment(s1, _FUTURE_DATE, "15:00")
    assert first["ok"] is True

    s2 = _fresh_session(stage="schedule_appointment", full_name="John Roe")
    second = realtime_tools.handle_schedule_appointment(s2, _FUTURE_DATE, "15:00")
    assert second["ok"] is False
    assert "already booked" in second["error"]
    # the second caller's own session must NOT have been advanced/submitted
    assert s2.stage == "schedule_appointment"


# ---- call_tool dispatch + error containment --------------------------------

def test_call_tool_dispatches_to_the_right_handler():
    s = _fresh_session(stage="full_name")
    result = realtime_tools.call_tool(s, "confirm_slot", {"field": "full_name", "value": "Jane Doe"})
    assert result["ok"] is True
    assert s.slots["full_name"] == "Jane Doe"


def test_call_tool_unknown_tool_name_returns_error_not_exception():
    s = _fresh_session(stage="full_name")
    result = realtime_tools.call_tool(s, "delete_all_leads", {})
    assert result["ok"] is False
    assert "unknown tool" in result["error"]


def test_call_tool_missing_arguments_defaults_to_empty_dict():
    s = _fresh_session(stage="full_name")
    result = realtime_tools.call_tool(s, "confirm_slot", None)
    assert result["ok"] is False  # missing field/value, but must not raise


def test_call_tool_swallows_handler_exceptions(monkeypatch):
    s = _fresh_session(stage="full_name")

    def _boom(session, args):
        raise RuntimeError("simulated failure")

    monkeypatch.setitem(realtime_tools._DISPATCH, "confirm_slot", _boom)
    result = realtime_tools.call_tool(s, "confirm_slot", {"field": "full_name", "value": "x"})
    assert result["ok"] is False
    assert "internal error" in result["error"]


# ---- tools_for_stage gating -------------------------------------------------

def test_tools_for_stage_base_tools_always_present():
    names = {t["name"] for t in realtime_tools.tools_for_stage(_fresh_session("full_name"))}
    assert names == {"confirm_slot", "save_lead_to_db", "go_back_or_edit"}


def test_tools_for_stage_pricing_tool_only_where_relevant():
    for stage in ("location", "plan_choice", "plan_action"):
        names = {t["name"] for t in realtime_tools.tools_for_stage(_fresh_session(stage))}
        assert "get_plan_pricing" in names
    names = {t["name"] for t in realtime_tools.tools_for_stage(_fresh_session("full_name"))}
    assert "get_plan_pricing" not in names


def test_tools_for_stage_schedule_tool_only_at_schedule_stage():
    names = {t["name"] for t in realtime_tools.tools_for_stage(_fresh_session("schedule_appointment"))}
    assert "schedule_appointment" in names
    names = {t["name"] for t in realtime_tools.tools_for_stage(_fresh_session("tonnage"))}
    assert "schedule_appointment" not in names


def test_confirm_slot_schema_enum_locks_category_values():
    """The actual regression test for the confirmation-loop bug: the
    model must be given the exact valid ids for a choice field, not a
    free string it has to guess/invent."""
    tools = realtime_tools.tools_for_stage(_fresh_session("category"))
    schema = next(t for t in tools if t["name"] == "confirm_slot")
    value_prop = schema["parameters"]["properties"]["value"]
    assert set(value_prop["enum"]) == {"heating", "cooling_electric_heat", "cooling_heat_pump"}
    assert schema["parameters"]["properties"]["field"]["enum"] == ["category"]


def test_confirm_slot_schema_free_text_for_non_choice_stage():
    tools = realtime_tools.tools_for_stage(_fresh_session("full_name"))
    schema = next(t for t in tools if t["name"] == "confirm_slot")
    value_prop = schema["parameters"]["properties"]["value"]
    assert "enum" not in value_prop


# ---- get_plan_pricing / save_lead_to_db ------------------------------------

def test_get_plan_pricing_returns_real_plans_only():
    s = _fresh_session(stage="location")
    result = realtime_tools.handle_get_plan_pricing(s, "cooling_electric_heat", "2_ton", "attic_horizontal")
    ids = {p["id"] for p in result["plans"]}
    assert ids == {"cooling_better", "heating_better", "cooling_best"}


def test_get_plan_pricing_unknown_combo_returns_empty_not_error():
    s = _fresh_session(stage="location")
    result = realtime_tools.handle_get_plan_pricing(s, "heating", "2_ton", "attic_horizontal")
    assert result["plans"] == []


def test_save_lead_to_db_does_not_raise_without_storage_redirect(redirect_storage):
    s = _fresh_session(stage="tonnage", full_name="Jane Doe")
    result = realtime_tools.handle_save_lead_to_db(s, is_complete=False)
    assert result == {"ok": True}


# ---- confirm_slot: plan_action + co-answered call_timing -------------------

def _to_plan_action(s):
    """Drives an already-at-location session through plan_choice and
    review_summary into plan_action, using the same handle_manual
    pattern as the rest of this file."""
    d = DialogueManager(s)
    d.handle_manual("plan_choice", ["cooling_better"])
    if s.stage == "review_summary":
        d.handle_manual("review_summary", "confirmed")
    assert s.stage == "plan_action"
    return s


def test_confirm_slot_plan_action_with_valid_co_answered_call_timing_chains_in_one_call(redirect_storage):
    s = _to_stage("location")
    DialogueManager(s).handle_manual("location", "attic_horizontal")
    _to_plan_action(s)
    result = realtime_tools.handle_confirm_slot(s, "plan_action", "arrange_call", call_timing="immediate")
    assert result["ok"] is True
    assert result["stage"] == "closing"
    assert s.slots["call_timing"] == "immediate"


def test_confirm_slot_plan_action_without_call_timing_still_advances_normally(redirect_storage):
    s = _to_stage("location")
    DialogueManager(s).handle_manual("location", "attic_horizontal")
    _to_plan_action(s)
    result = realtime_tools.handle_confirm_slot(s, "plan_action", "arrange_call")
    assert result["ok"] is True
    assert result["stage"] == "call_timing"
    assert "call_timing" not in s.slots


def test_confirm_slot_plan_action_ignores_invalid_call_timing_instead_of_erroring(redirect_storage):
    s = _to_stage("location")
    DialogueManager(s).handle_manual("location", "attic_horizontal")
    _to_plan_action(s)
    result = realtime_tools.handle_confirm_slot(s, "plan_action", "arrange_call", call_timing="not_a_real_timing")
    assert result["ok"] is True
    assert result["stage"] == "call_timing"  # plan_action itself still saved
    assert "call_timing" not in s.slots


def test_confirm_slot_schema_exposes_call_timing_only_at_plan_action():
    s = _fresh_session(stage="plan_action")
    tools = realtime_tools.tools_for_stage(s)
    schema = next(t for t in tools if t["name"] == "confirm_slot")
    assert "call_timing" in schema["parameters"]["properties"]
    assert "call_timing" not in schema["parameters"]["required"]

    other = realtime_tools.tools_for_stage(_fresh_session("phone"))
    schema2 = next(t for t in other if t["name"] == "confirm_slot")
    assert "call_timing" not in schema2["parameters"]["properties"]