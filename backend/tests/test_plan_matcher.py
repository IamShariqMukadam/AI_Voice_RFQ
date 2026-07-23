import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import plan_matcher


def test_known_combo_returns_three_plans():
    plans = plan_matcher.get_available_plans("cooling_electric_heat", "2_ton", "attic_horizontal")
    assert len(plans) == 3
    assert {p["id"] for p in plans} == {"cooling_better", "heating_better", "cooling_best"}


def test_garage_returns_no_plans():
    plans = plan_matcher.get_available_plans("cooling_electric_heat", "2_ton", "garage_vertical")
    assert plans == []


def test_4_ton_now_has_plans_from_csv():
    plans = plan_matcher.get_available_plans("cooling_electric_heat", "4_ton", "attic_horizontal")
    assert len(plans) == 3


def test_unknown_tonnage_returns_empty_not_error():
    plans = plan_matcher.get_available_plans("cooling_electric_heat", "5_ton", "attic_horizontal")
    assert plans == []


def test_unknown_category_returns_empty_not_error():
    plans = plan_matcher.get_available_plans("heating", "2_ton", "attic_horizontal")
    assert plans == []