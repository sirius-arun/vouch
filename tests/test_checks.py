"""Tests for the deterministic rule checks."""

import pytest
from pydantic import ValidationError

from vouch.checks import describe, run_check, run_checks
from vouch.schemas import Check


def check(**kwargs) -> Check:
    """Build a Check from YAML-style data (shorthand or full form)."""
    return Check.model_validate(kwargs)


# --- contains / not_contains -------------------------------------------------


def test_contains_pass_and_fail():
    assert run_check(check(contains="14 days"), "Refunds within 14 days.").passed
    result = run_check(check(contains="14 days"), "Refunds within 30 days.")
    assert not result.passed
    assert "missing" in result.detail


def test_contains_is_case_insensitive_by_default():
    assert run_check(check(contains="ACME"), "Welcome to acme cloud").passed


def test_contains_case_sensitive_option():
    c = check(type="contains", value="ACME", case_sensitive=True)
    assert not run_check(c, "Welcome to acme cloud").passed
    assert run_check(c, "Welcome to ACME cloud").passed


def test_not_contains():
    assert run_check(check(not_contains="ticket #"), "Happy to help!").passed
    result = run_check(check(not_contains="ticket #"), "Per Ticket #123 notes...")
    assert not result.passed
    assert "char 4" in result.detail


# --- regex ---------------------------------------------------------------------


def test_regex_match_and_no_match():
    c = check(regex=r"14[- ]days?")
    assert run_check(c, "You have a 14-day window.").passed
    assert run_check(c, "within 14 days").passed
    assert not run_check(c, "within 30 days").passed


def test_regex_is_case_sensitive_unless_flagged():
    assert not run_check(check(regex="refund"), "REFUND").passed
    assert run_check(check(regex="(?i)refund"), "REFUND").passed


def test_regex_reports_matched_text():
    assert run_check(check(regex=r"\d+ GB"), "Free plan: 5 GB").detail == 'matched "5 GB"'


def test_invalid_regex_rejected_at_load():
    with pytest.raises(ValidationError, match="invalid regex"):
        check(regex="(unclosed")


# --- max_chars -----------------------------------------------------------------


def test_max_chars_boundary():
    assert run_check(check(max_chars=5), "12345").passed
    result = run_check(check(max_chars=5), "123456")
    assert not result.passed
    assert result.detail == "6 chars > 5"


def test_max_chars_needs_int():
    with pytest.raises(ValidationError):
        check(max_chars="five")
    with pytest.raises(ValidationError):
        check(max_chars=-1)
    with pytest.raises(ValidationError):
        check(max_chars=True)


# --- json_valid ----------------------------------------------------------------


@pytest.mark.parametrize("text", ['{"plan": "Pro"}', "[1, 2]", '  {"a": null}\n', "42"])
def test_json_valid_accepts_json(text):
    assert run_check(check(json_valid=True), text).passed


@pytest.mark.parametrize("text", ["", "not json", '```json\n{"a": 1}\n```', "{'a': 1}"])
def test_json_valid_rejects_non_json(text):
    result = run_check(check(json_valid=True), text)
    assert not result.passed
    assert result.detail.startswith("invalid JSON")


def test_json_valid_takes_no_value():
    assert check(json_valid=True).value is None
    with pytest.raises(ValidationError):
        check(json_valid="yes")


# --- schema forms and helpers --------------------------------------------------


def test_shorthand_and_full_form_are_equivalent():
    assert check(contains="x") == check(type="contains", value="x")


def test_string_checks_need_non_empty_value():
    for kind in ("contains", "not_contains", "regex"):
        with pytest.raises(ValidationError):
            check(**{kind: ""})
        with pytest.raises(ValidationError):
            check(**{kind: 5})


def test_unknown_check_type_rejected():
    with pytest.raises(ValidationError):
        check(starts_with="Hi")


def test_run_checks_returns_one_result_per_check():
    checks = [check(contains="Pro"), check(max_chars=3), check(json_valid=True)]
    results = run_checks(checks, '{"plan": "Pro"}')
    assert [r.passed for r in results] == [True, False, True]


def test_describe_labels():
    assert describe(check(contains="14 days")) == 'contains "14 days"'
    assert describe(check(max_chars=600)) == "max_chars 600"
    assert describe(check(json_valid=True)) == "json_valid"
