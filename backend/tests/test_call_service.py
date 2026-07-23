import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.call_service import _to_e164


def test_bare_local_number_gets_country_code_prepended():
    assert _to_e164("9876543210", "+91") == "+919876543210"
    assert _to_e164("5551234567", "+1") == "+15551234567"


def test_already_e164_number_passes_through_untouched():
    # Guards against double-prefixing if clean_phone's contract ever
    # changes to include a "+" - this must never become "+91+919876543210".
    assert _to_e164("+919876543210", "+91") == "+919876543210"


def test_empty_phone_passes_through_unchanged():
    assert _to_e164("", "+91") == ""
    assert _to_e164(None, "+91") == ""


def test_no_country_code_passed_falls_back_to_plus_91():
    # call_service.trigger_immediate_call always passes
    # config.DEFAULT_COUNTRY_CODE explicitly, so this only matters for a
    # direct call to _to_e164() with no second argument - now defaults
    # to +91 rather than leaving the number unprefixed.
    assert _to_e164("9876543210") == "+919876543210"
