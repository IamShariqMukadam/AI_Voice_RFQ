import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import SessionState
from dialogue.state_machine import DialogueManager


def _fill_personal_info(d):
    d.greeting()
    d.handle_manual("full_name", "Shariq Mukadam")
    d.handle_manual("phone", "7378850558")
    d.handle_manual("email", "shariq@gmail.com")
    d.handle_manual("street", "123")
    d.handle_manual("city", "Pune")
    d.handle_manual("zip", "411048")


def test_editing_personal_info_field_resumes_where_it_left_off():
    s = SessionState()
    d = DialogueManager(s)
    _fill_personal_info(d)
    d.handle_manual("category", "cooling_electric_heat")
    d.handle_manual("tonnage", "2_ton")
    assert s.stage == "location"
    d.jump_to("phone")
    d.handle_manual("phone", "9999999999")
    assert s.stage == "location"  # resumed, not reset back to "email"
    assert s.slots["phone"] == "9999999999"


def test_review_summary_needs_changes_gives_short_prompt_not_full_dump():
    s = SessionState()
    d = DialogueManager(s)
    _fill_personal_info(d)
    d.handle_manual("category", "cooling_electric_heat")
    d.handle_manual("tonnage", "2_ton")
    d.handle_manual("location", "attic_horizontal")
    d.handle_manual("plan_choice", ["cooling_better"])
    assert s.stage == "review_summary"
    display, speech = d.handle_manual("review_summary", "needs_changes")
    assert s.stage == "review_summary"  # stays put, doesn't advance to plan_action
    assert "what did i get wrong" in speech.lower()
    assert "Full Name:" not in display  # short prompt, not the whole summary again
    s = SessionState()
    d = DialogueManager(s)
    _fill_personal_info(d)
    d.handle_manual("category", "cooling_electric_heat")
    d.handle_manual("tonnage", "2_ton")
    d.handle_manual("location", "attic_horizontal")
    assert s.stage == "plan_choice"
    d.jump_to("category")
    d.handle_manual("category", "heating")  # heating has no instant plans
    assert s.stage == "closing"  # correctly re-derived, not stuck resuming plan_choice


def test_voice_edit_command_jumps_to_the_right_field():
    s = SessionState()
    d = DialogueManager(s)
    _fill_personal_info(d)
    d.handle_manual("category", "cooling_electric_heat")
    assert s.stage == "tonnage"
    display, _ = d.handle_turn("can you change my email please")
    assert s.stage == "email"
    d.handle_manual("email", "correct@gmail.com")
    assert s.stage == "tonnage"  # resumed
    assert s.slots["email"] == "correct@gmail.com"


def test_voice_edit_command_on_unanswered_field_is_declined():
    s = SessionState()
    d = DialogueManager(s)
    d.greeting()
    d.handle_manual("full_name", "Shariq Mukadam")
    display, _ = d.handle_turn("can you change my plan")
    assert s.stage == "phone"  # unchanged
    assert "haven't gotten to that" in display


def test_phone_accepts_any_spoken_ten_digit_number():
    s = SessionState()
    d = DialogueManager(s)
    d.greeting()
    d.handle_manual("full_name", "Azhar Khan")

    d.handle_turn("nine eight seven six five four three two one zero")

    assert s.slots["phone"] == "9876543210"
    assert s.stage == "email"


def test_phone_combines_short_digit_fragment_with_next_turn():
    s = SessionState()
    d = DialogueManager(s)
    d.greeting()
    d.handle_manual("full_name", "Azhar Khan")

    display, _ = d.handle_turn("one, two, three, four, five, six, one, two, three")
    assert s.stage == "phone"
    assert "remaining 1 digit" in display
    assert "phone" not in s.slots

    d.handle_turn("zero")

    assert s.slots["phone"] == "1234561230"
    assert s.stage == "email"