"""Deterministic rule checks: contains, not_contains, regex, max_chars, json_valid."""

import json
import re
from dataclasses import dataclass

from .schemas import Check


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one check on one output."""

    check: Check
    passed: bool
    detail: str

    def to_dict(self) -> dict:
        return {
            "type": self.check.type,
            "value": self.check.value,
            "label": describe(self.check),
            "passed": self.passed,
            "detail": self.detail,
        }


def describe(check: Check) -> str:
    """Short human label, e.g. `contains "14 days"`."""
    if check.value is None:
        return check.type
    if isinstance(check.value, str):
        return f'{check.type} "{check.value}"'
    return f"{check.type} {check.value}"


def _find(needle: str, haystack: str, case_sensitive: bool) -> int:
    """Index of needle in haystack (or -1), optionally ignoring case."""
    if case_sensitive:
        return haystack.find(needle)
    return haystack.casefold().find(needle.casefold())


def run_check(check: Check, output: str) -> CheckResult:
    """Run a single check against an output string."""
    match check.type:
        case "contains":
            found = _find(check.value, output, check.case_sensitive) >= 0
            return CheckResult(check, found, "found" if found else f'missing "{check.value}"')
        case "not_contains":
            idx = _find(check.value, output, check.case_sensitive)
            if idx < 0:
                return CheckResult(check, True, "not found")
            return CheckResult(check, False, f'found forbidden text at char {idx}: "{check.value}"')
        case "regex":
            m = re.search(check.value, output)
            if m:
                return CheckResult(check, True, f'matched "{m.group(0)}"')
            return CheckResult(check, False, "no match")
        case "max_chars":
            n = len(output)
            ok = n <= check.value
            return CheckResult(check, ok, f"{n} chars" + ("" if ok else f" > {check.value}"))
        case "json_valid":
            try:
                json.loads(output)
            except json.JSONDecodeError as e:
                return CheckResult(check, False, f"invalid JSON: {e}")
            return CheckResult(check, True, "valid JSON")
    raise ValueError(f"unknown check type: {check.type}")  # unreachable: schema validates


def run_checks(checks: list[Check], output: str) -> list[CheckResult]:
    """Run every check against an output."""
    return [run_check(c, output) for c in checks]
