"""Tests for judge parsing and evidence validation, using a fake provider (no API calls)."""

import asyncio
import json

import pytest

from vouch.checks import CheckResult
from vouch.grading import TrialGrade
from vouch.judge import (
    EVIDENCE_NOT_FOUND,
    NO_EVIDENCE,
    Judge,
    JudgeError,
    Judgment,
    evidence_in_output,
    parse_judgment,
    validate_evidence,
)
from vouch.runner import TrialOutput
from vouch.schemas import Check, ContextPack, Requirement

OUTPUT = "Thanks for asking!\nRefunds are available within   60 days of purchase.\nAnything else?"
CONTEXT = ContextPack(
    product="Acme Cloud support bot",
    requirements=[Requirement(id="R1", text="States the 14-day refund window.", severity="high")],
    facts=["Refunds within 14 days."],
)
REQ = CONTEXT.requirements[0]


class FakeProvider:
    """Returns canned replies in order and records the prompts it was sent."""

    name = "fake"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def complete(self, prompt: str, model: str, temperature: float) -> str:
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def aclose(self) -> None:
        pass


def reply(verdict: str, evidence: str = "", reason: str = "because") -> str:
    return json.dumps({"verdict": verdict, "requirement_id": "R1", "evidence": evidence, "reason": reason})


def run_judge(provider: FakeProvider, output: str = OUTPUT) -> Judgment:
    return asyncio.run(Judge(provider, model="fake-model").judge(CONTEXT, REQ, "How long for refunds?", output))


# --- evidence matching ---------------------------------------------------------


def test_evidence_exact_substring():
    assert evidence_in_output("Anything else?", OUTPUT)


def test_evidence_ignores_whitespace_differences():
    # The output has a newline and triple space; the quote has single spaces.
    assert evidence_in_output("Thanks for asking! Refunds are available within 60 days", OUTPUT)
    assert evidence_in_output("  within\n60   days ", OUTPUT)


def test_evidence_accepts_surrounding_quote_marks():
    assert evidence_in_output('"within 60 days of purchase."', OUTPUT)
    assert evidence_in_output("“within 60 days”", OUTPUT)


def test_evidence_rejects_paraphrase_and_case_changes():
    assert not evidence_in_output("Refunds are available for 60 days", OUTPUT)  # paraphrase
    assert not evidence_in_output("refunds are available within 60 days", OUTPUT)  # case changed
    assert not evidence_in_output("", OUTPUT)


# --- validate_evidence ----------------------------------------------------------


def test_fail_with_real_evidence_stays_fail():
    j = validate_evidence(Judgment("R1", "fail", "within 60 days", "wrong window"), OUTPUT)
    assert (j.verdict, j.reason, j.evidence_found) == ("fail", "wrong window", True)


def test_fail_with_fabricated_evidence_becomes_unknown():
    j = Judgment("R1", "fail", "Refunds last 90 days", "wrong window", raw_verdict="fail")
    j = validate_evidence(j, OUTPUT)
    assert j.verdict == "unknown"
    assert j.reason == EVIDENCE_NOT_FOUND
    assert j.raw_verdict == "fail"  # the judge's original verdict is kept for auditing
    assert j.evidence_found is False


def test_fail_without_evidence_becomes_unknown():
    j = validate_evidence(Judgment("R1", "fail", "", "wrong window"), OUTPUT)
    assert (j.verdict, j.reason, j.evidence_found) == ("unknown", NO_EVIDENCE, None)


def test_pass_is_not_downgraded():
    j = validate_evidence(Judgment("R1", "pass", "", "fine"), OUTPUT)
    assert j.verdict == "pass"
    j = validate_evidence(Judgment("R1", "pass", "not in output", "fine"), OUTPUT)
    assert (j.verdict, j.evidence_found) == ("pass", False)


def test_unknown_stays_unknown():
    j = validate_evidence(Judgment("R1", "unknown", "", "borderline"), OUTPUT)
    assert (j.verdict, j.reason) == ("unknown", "borderline")


# --- parse_judgment -------------------------------------------------------------


def test_parse_handles_code_fences_and_case():
    j = parse_judgment('```json\n{"verdict": "FAIL", "evidence": "x", "reason": "r"}\n```', "R1")
    assert (j.verdict, j.evidence, j.requirement_id) == ("fail", "x", "R1")


def test_parse_uses_requested_requirement_id():
    j = parse_judgment('{"verdict": "pass", "requirement_id": "R9", "evidence": "", "reason": ""}', "R1")
    assert j.requirement_id == "R1"


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '{"verdict": "maybe"}', '{"verdict": "pass", "evidence": 3}'])
def test_parse_rejects_bad_replies(text):
    with pytest.raises(JudgeError):
        parse_judgment(text, "R1")


# --- Judge end to end with a mocked provider ------------------------------------


def test_judge_fail_with_verified_evidence():
    j = run_judge(FakeProvider(reply("fail", "within 60 days of purchase", "Says 60 days, policy is 14.")))
    assert j.verdict == "fail"
    assert j.evidence_found is True


def test_judge_fail_with_hallucinated_evidence_becomes_unknown():
    j = run_judge(FakeProvider(reply("fail", "Refunds are available within 30 days", "Wrong window.")))
    assert j.verdict == "unknown"
    assert j.reason == EVIDENCE_NOT_FOUND
    assert j.raw_verdict == "fail"


def test_judge_retries_once_on_bad_json():
    provider = FakeProvider("Sure! The verdict is fail.", reply("pass"))
    j = run_judge(provider)
    assert j.verdict == "pass"
    assert len(provider.prompts) == 2
    assert "not valid JSON" in provider.prompts[1]


def test_judge_gives_up_after_two_bad_replies():
    j = run_judge(FakeProvider("nope", "still nope"))
    assert j.verdict == "unknown"
    assert j.error and "bad JSON twice" in j.error


def test_judge_api_failure_becomes_unknown():
    j = run_judge(FakeProvider(RuntimeError("rate limited")))
    assert j.verdict == "unknown"
    assert "RuntimeError" in j.error


def test_judge_prompt_contains_requirement_input_and_output():
    provider = FakeProvider(reply("pass"))
    run_judge(provider)
    prompt = provider.prompts[0]
    assert "R1 (severity: high): States the 14-day refund window." in prompt
    assert "How long for refunds?" in prompt
    assert OUTPUT in prompt
    assert "Refunds within 14 days." in prompt


# --- trial pass rule ------------------------------------------------------------


def grade(checks_pass: list[bool], verdicts: list[str]) -> TrialGrade:
    out = TrialOutput("c1", 0, "q", "a", None, 1, 0.1)
    checks = [CheckResult(Check(type="contains", value="a"), ok, "") for ok in checks_pass]
    judgments = [Judgment("R1", v, "", "") for v in verdicts]
    return TrialGrade(out, checks, judgments)


def test_trial_passes_only_if_all_checks_and_judgments_pass():
    assert grade([True, True], ["pass", "pass"]).passed
    assert not grade([True, False], ["pass"]).passed
    assert not grade([True], ["pass", "fail"]).passed
    assert not grade([True], ["unknown"]).passed  # unknown is not a pass: it goes to a human
