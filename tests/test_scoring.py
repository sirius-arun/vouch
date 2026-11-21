"""Tests for pass^k, rollups, seeded-defect metrics and report building."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from vouch.checks import run_checks
from vouch.config import Suite
from vouch.grading import TrialGrade, write_grades
from vouch.judge import Judgment
from vouch.report import build_report, write_run_meta
from vouch.runner import TrialOutput
from vouch.schemas import CaseSuite, Config, ContextPack, HttpConfig
from vouch.scoring import defects_from_info, pass_hat_k, resolve_defects, score
from vouch.targets.http import HttpTarget

# --- pass^k estimator ----------------------------------------------------------


def test_pass_hat_k_example():
    assert pass_hat_k(n=5, c=4, k=2) == pytest.approx(0.6)  # C(4,2)/C(5,2) = 6/10


def test_pass_hat_k_all_pass_is_one():
    for n in range(1, 8):
        for k in range(1, n + 1):
            assert pass_hat_k(n, n, k) == 1.0


def test_pass_hat_k_fewer_passes_than_k_is_zero():
    assert pass_hat_k(n=5, c=1, k=2) == 0.0
    assert pass_hat_k(n=5, c=4, k=5) == 0.0
    assert pass_hat_k(n=5, c=0, k=1) == 0.0


def test_pass_hat_1_is_the_pass_rate():
    assert pass_hat_k(n=5, c=3, k=1) == pytest.approx(0.6)


def test_pass_hat_k_falls_as_k_grows():
    values = [pass_hat_k(10, 8, k) for k in range(1, 11)]
    assert values == sorted(values, reverse=True)


@pytest.mark.parametrize("n,c,k", [(5, 4, 0), (5, 4, 6), (0, 0, 1), (5, 6, 2), (5, -1, 2)])
def test_pass_hat_k_rejects_bad_arguments(n, c, k):
    with pytest.raises(ValueError):
        pass_hat_k(n, c, k)


# --- a tiny suite ---------------------------------------------------------------

CONTEXT = ContextPack.model_validate({
    "product": "Demo bot",
    "requirements": [
        {"id": "R1", "text": "States the 14-day window.", "severity": "high"},
        {"id": "R2", "text": "Is polite.", "severity": "medium"},
    ],
})
CASES = CaseSuite.model_validate({"cases": [
    {"id": "refund", "input": "refund?", "requirement_ids": ["R1"], "checks": [{"contains": "14"}],
     "tags": ["wrong_refund"]},
    {"id": "tone", "input": "hi", "requirement_ids": ["R2"], "tags": ["rude"]},
    {"id": "clean-a", "input": "plans?", "requirement_ids": ["R1"], "tags": ["clean"]},
    {"id": "clean-b", "input": "price?", "checks": [{"contains": "$10"}], "tags": ["clean"]},
    {"id": "ungraded", "input": "hello"},
]}).cases
CONFIG = Config.model_validate({
    "target": {"type": "http", "url": "http://localhost:1/chat"},
    "seeded_defects": ["wrong_refund", "rude"],
})
SUITE = Suite(dir=Path("."), context=CONTEXT, cases=CASES, config=CONFIG)

# (case_id, outputs per trial, judge verdicts per trial)
TRIALS = {
    "refund": (["14 days", "60 days", "14 days", "14 days", "60 days"], ["pass", "fail", "pass", "pass", "fail"]),
    "tone": (["Hi!"] * 5, ["pass", "pass", "pass", "pass", "unknown"]),
    "clean-a": (["ok"] * 5, ["pass"] * 5),
    "clean-b": (["$10", "$10", "$10", "$10", "$12"], [None] * 5),
    "ungraded": (["hey"] * 5, [None] * 5),
}


def make_grades() -> list[TrialGrade]:
    cases = {c.id: c for c in CASES}
    grades = []
    for cid, (outputs, verdicts) in TRIALS.items():
        case = cases[cid]
        for i, (out, verdict) in enumerate(zip(outputs, verdicts)):
            output = TrialOutput(cid, i, case.input, out, None, 1, 0.1)
            judgments = [
                Judgment(rid, verdict, out if verdict == "fail" else "", "r", raw_verdict=verdict)
                for rid in case.requirement_ids
            ] if verdict else []
            grades.append(TrialGrade(output, run_checks(case.checks, out), judgments))
    return grades


def test_case_scores():
    s = score(SUITE, make_grades(), judged=True, active_defects=["wrong_refund", "rude"])
    by_id = {c.case_id: c for c in s.cases}
    refund = by_id["refund"]
    assert (refund.n, refund.passed, refund.pass_rate) == (5, 3, 0.6)
    assert refund.pass_n == 0.0
    assert by_id["clean-a"].pass_n == 1.0
    assert by_id["tone"].passed == 4  # the 'unknown' trial does not pass
    assert by_id["tone"].judgments()["unknown"] == 1
    assert not by_id["ungraded"].graded
    assert s.k == 5
    assert len(s.graded) == 4
    assert s.pass_rate == pytest.approx((0.6 + 0.8 + 1.0 + 0.8) / 4)


def test_rollups_by_requirement_and_severity():
    s = score(SUITE, make_grades(), judged=True, active_defects=[])
    r1 = next(r for r in s.requirements if r.name == "R1")
    assert (r1.cases, r1.trials, r1.passed) == (2, 10, 8)  # refund + clean-a
    assert r1.judgments == {"pass": 8, "fail": 2}
    high = next(r for r in s.severities if r.name == "high")
    assert high.description == "R1" and high.cases == 2
    assert [r.name for r in s.severities] == ["high", "medium"]


def test_defect_metrics_all_active():
    d = score(SUITE, make_grades(), judged=True, active_defects=["wrong_refund", "rude"]).defects
    assert d.mode == "seeded"
    assert d.defect_cases == ["refund", "tone"]
    assert d.detected == ["refund", "tone"]  # tone is caught via its 'unknown' trial
    assert d.detection_rate == 1.0
    assert d.control_cases == ["clean-a", "clean-b"]  # every graded case without an active defect
    assert d.false_positives == ["clean-b"]
    assert d.false_positive_rate == 0.5


def test_inactive_defect_cases_become_controls():
    d = score(SUITE, make_grades(), judged=True, active_defects=["rude"]).defects
    assert d.defect_cases == ["tone"]
    assert [x.name for x in d.per_defect] == ["rude"]
    # 'refund' targets an inactive defect, so it should pass: its failures are false positives.
    assert d.control_cases == ["refund", "clean-a", "clean-b"]
    assert d.false_positives == ["refund", "clean-b"]


def test_clean_run_has_no_detection_and_counts_all_cases():
    d = score(SUITE, make_grades(), judged=True, active_defects=[]).defects
    assert d.mode == "clean"
    assert d.detection_rate is None and d.per_defect == []
    assert d.control_cases == ["refund", "tone", "clean-a", "clean-b"]  # all graded cases
    assert d.false_positives == ["refund", "tone", "clean-b"]
    assert d.false_positive_rate == 0.75


def test_unknown_defects_use_clean_tagged_cases_only():
    d = score(SUITE, make_grades(), judged=True, active_defects=None).defects
    assert d.mode == "unknown"
    assert d.detection_rate is None
    assert d.control_cases == ["clean-a", "clean-b"]
    assert d.false_positive_rate == 0.5


def test_defects_from_info():
    assert defects_from_info({"model": "m", "defects": ["a", "b"]}) == ["a", "b"]
    assert defects_from_info({"defects": []}) == []
    assert defects_from_info({"model": "m"}) is None
    assert defects_from_info({"defects": "a,b"}) is None  # wrong type: not trusted
    assert defects_from_info(None) is None


def test_http_target_reads_health_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/health"
        return httpx.Response(200, json={"model": "gpt-4o-mini", "defects": ["rude"]})

    target = HttpTarget(HttpConfig(type="http", url="http://bot.local:8000/chat?x=1"), timeout_s=5)
    assert target.health_url == "http://bot.local:8000/health"
    target.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(target.info()) == {"model": "gpt-4o-mini", "defects": ["rude"]}


def test_http_target_info_is_none_when_health_missing():
    target = HttpTarget(HttpConfig(type="http", url="http://bot.local/chat"), timeout_s=5)
    target.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert asyncio.run(target.info()) is None


def test_without_judge_only_rule_checked_cases_are_graded():
    grades = make_grades()
    for g in grades:
        g.judgments = []
    s = score(SUITE, grades, judged=False, active_defects=[])
    assert {c.case_id for c in s.graded} == {"refund", "clean-b"}


def test_resolve_defects():
    assert resolve_defects("all", CONFIG) == ["wrong_refund", "rude"]
    assert resolve_defects("none", CONFIG) == []
    assert resolve_defects(" rude ,", CONFIG) == ["rude"]
    with pytest.raises(ValueError, match="unknown defects"):
        resolve_defects("typo", CONFIG)


# --- report from saved files ------------------------------------------------------


def test_build_report_from_run_folder(tmp_path: Path):
    run_dir = tmp_path / "demo" / "runs" / "20260101-120000"
    run_dir.mkdir(parents=True)
    (run_dir / "context.yaml").write_text(json.dumps(CONTEXT.model_dump()))  # JSON is valid YAML
    (run_dir / "cases.yaml").write_text(json.dumps({"cases": [c.model_dump(exclude_none=True) for c in CASES]}))
    (run_dir / "config.yaml").write_text(json.dumps(CONFIG.model_dump()))
    grades = make_grades()
    with (run_dir / "outputs.jsonl").open("w") as f:
        for g in grades:
            f.write(json.dumps(g.output.__dict__) + "\n")
    write_grades(run_dir / "grades.jsonl", grades)
    write_run_meta(run_dir, {"run_id": run_dir.name, "suite": "demo", "started_at": "2026-01-01T12:00:00",
                             "target": "http:x", "target_type": "http", "app_model": "m", "judge_model": "j",
                             "active_defects": ["wrong_refund", "rude"]})

    scores, md_path, html_path = build_report(run_dir)
    assert scores.defects.detection_rate == 1.0
    md, html = md_path.read_text(), html_path.read_text()
    for text in (md, html):
        assert "pass^5" in text and "2 / 2 cases targeting an active defect caught" in text
        assert "refund" in text and "clean-b" in text
    assert "60 days" in md  # failure evidence is quoted
    assert "<script" not in html and "http://" not in html.split("</style>")[0]  # self-contained

    # Overriding the active defects changes the metrics and is saved to run.json.
    scores, md_path, html_path = build_report(run_dir, defects="none")
    assert scores.defects.mode == "clean"
    assert "No seeded defects active (clean run)" in md_path.read_text()
    assert "No seeded defects active (clean run)" in html_path.read_text()
    assert "Seeded-defect detection" not in md_path.read_text()
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["active_defects"] == [] and meta["defects_source"].startswith("override")
    scores, _, _ = build_report(run_dir)  # a later rebuild keeps the override
    assert scores.defects.mode == "clean"
