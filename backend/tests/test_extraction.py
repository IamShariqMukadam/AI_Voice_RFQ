import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.extraction import clean_email, clean_name, clean_phone, clean_zip, match_choice


def test_clean_email_spoken_form():
    assert clean_email("john dot smith at gmail dot com") == "john.smith@gmail.com"


def test_clean_email_literal_form():
    assert clean_email("john.smith@gmail.com") == "john.smith@gmail.com"


def test_clean_email_rejects_garbage():
    assert clean_email("uhh not sure") is None


def test_clean_phone_spoken_digits():
    assert clean_phone("nine one two five five five one two three four") == "9125551234"
    assert clean_phone("seven, three, seven, eight, eight, five, zero, five, five, eight") == "7378850558"


def test_clean_phone_literal_digits():
    assert clean_phone("call me at 9125551234") == "9125551234"


def test_clean_zip():
    assert clean_zip("three two seven zero three") == "32703"


def test_clean_name_strips_filler():
    assert clean_name("my name is Shariq") == "Shariq"
    assert clean_name("it's Mukadam") == "Mukadam"


def test_clean_name_applies_spelling_hint():
    assert clean_name("my name is Sharik with q instead of k") == "Shariq"
    assert clean_name("My name is Sharik with a Q Mukadam") == "Shariq Mukadam"
    assert clean_name("my name is sharik mukadam with q instead of k") == "Shariq Mukadam"


def test_match_choice_exact_substring():
    opts = [{"value": "2_ton", "label": "2 Ton"}, {"value": "3_ton", "label": "3 Tons"}]
    assert match_choice("I have a 2 ton unit", opts) == "2_ton"


def test_match_choice_no_match_returns_none():
    opts = [{"value": "2_ton", "label": "2 Ton"}, {"value": "3_ton", "label": "3 Tons"}]
    assert match_choice("completely unrelated sentence", opts) is None


def test_clean_phone_homophones_and_multipliers():
    assert clean_phone("for one won eight two zero five five five zero") == "4118205550"
    assert clean_phone("double seven three eight two zero triple five zero") == "7738205550"


def test_clean_zip_tens_words():
    assert clean_zip("four one one, forty eight") == "41148"


def test_clean_email_idioms_and_provider_fixes():
    assert clean_email("shariq at the rate g mail dot com") == "shariq@gmail.com"
    assert clean_email("test plus tag at outlook dot com") == "test+tag@outlook.com"


def test_clean_email_handles_comma_transcripts_and_spelled_letters():
    assert clean_email("sharik, dot, mukadam, at, gmail, dot, com") == "sharik.mukadam@gmail.com"
    assert clean_email("s-h-a-r-i-q dot m-u-k-a-d-a-m at the rate gmail dot com") == "shariq.mukadam@gmail.com"
    assert clean_email("sharik dot mukadam at gmail dot com with q instead of k") == "shariq.mukadam@gmail.com"
    assert clean_email("sharik dot mukadam at gmail dot com with a q") == "shariq.mukadam@gmail.com"
    assert clean_email("sharikwithaq.mukadam at gmail.com") == "shariq.mukadam@gmail.com"


def test_match_choice_spoken_number_words():
    opts = [{"value": "2_ton", "label": "2 Ton"}, {"value": "2.5_ton", "label": "2.5 Ton"}, {"value": "4_ton", "label": "4 Ton"}]
    assert match_choice("it's two and a half ton", opts) == "2.5_ton"
    assert match_choice("i think four tons", opts) == "4_ton"


def test_match_choice_bare_number_matches_tonnage():
    opts = [{"value": "2_ton", "label": "2 Ton"}, {"value": "2.5_ton", "label": "2.5 Ton"}, {"value": "3_ton", "label": "3 Tons"}]
    assert match_choice("2", opts) == "2_ton"
    assert match_choice("3", opts) == "3_ton"


def test_match_choice_bare_place_name_matches_location():
    opts = [{"value": "attic_horizontal", "label": "Attic Horizontal"}, {"value": "closet_vertical", "label": "Closet (Vertical)"}, {"value": "garage_vertical", "label": "Garage"}]
    assert match_choice("attic", opts) == "attic_horizontal"
    assert match_choice("closet", opts) == "closet_vertical"
    assert match_choice("garage", opts) == "garage_vertical"


def test_match_choice_genuinely_ambiguous_answer_returns_none_not_a_guess():
    # "cooling" alone doesn't say electric-heat vs heat-pump - and "better"
    # alone doesn't say which plan - guessing wrong here is worse than
    # asking again, so both must come back None rather than picking one.
    cat_opts = [{"value": "heating", "label": "Heating"}, {"value": "cooling_electric_heat", "label": "Cooling with Electric Heat"}, {"value": "cooling_heat_pump", "label": "Cooling with Heat Pump"}]
    assert match_choice("cooling", cat_opts) is None
    plan_opts = [{"value": "cooling_better", "label": "Cooling - Better - $600 / 5 months"}, {"value": "heating_better", "label": "Heating - Better - $750 / month"}]
    assert match_choice("better", plan_opts) is None


def test_match_choice_full_label_still_disambiguates_shared_words():
    # regression check: "electric heat" and "heating" share the word "heat"/
    # "heating" is a prefix of it, but the full phrase should still resolve
    # to the electric-heat option, not tie with plain "Heating".
    cat_opts = [{"value": "heating", "label": "Heating"}, {"value": "cooling_electric_heat", "label": "Cooling with Electric Heat"}, {"value": "cooling_heat_pump", "label": "Cooling with Heat Pump"}]
    assert match_choice("cooling with electric heat", cat_opts) == "cooling_electric_heat"
