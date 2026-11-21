"""Combine rule checks and judge verdicts into per-trial grades, and save them."""

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .checks import CheckResult, run_checks
from .config import Suite
from .judge import Judge, Judgment
from .runner import TrialOutput

GRADES_FILE = "grades.jsonl"


@dataclass
class TrialGrade:
    """Everything known about one trial: its output, rule check results and judge verdicts."""

    output: TrialOutput
    checks: list[CheckResult] = field(default_factory=list)
    judgments: list[Judgment] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only if the call succeeded, every rule check passed and every judged requirement passed."""
        return (
            self.output.error is None
            and all(c.passed for c in self.checks)
            and all(j.verdict == "pass" for j in self.judgments)
        )

    def to_dict(self) -> dict:
        return {
            "case_id": self.output.case_id,
            "trial_idx": self.output.trial_idx,
            "passed": self.passed,
            "error": self.output.error,
            "checks": [c.to_dict() for c in self.checks],
            "judgments": [j.to_dict() for j in self.judgments],
        }


def apply_checks(suite: Suite, outputs: list[TrialOutput]) -> list[TrialGrade]:
    """Run each case's rule checks on its outputs. Returns grades in case order, then trial order."""
    cases = {c.id: c for c in suite.cases}
    order = {c.id: i for i, c in enumerate(suite.cases)}
    grades = []
    for out in sorted(outputs, key=lambda o: (order[o.case_id], o.trial_idx)):
        checks = run_checks(cases[out.case_id].checks, out.output) if out.output is not None else []
        grades.append(TrialGrade(out, checks))
    return grades


def judge_jobs(suite: Suite, grades: list[TrialGrade]) -> list[tuple[TrialGrade, str]]:
    """Every (trial, requirement_id) pair to judge: trials with output x the case's requirements."""
    cases = {c.id: c for c in suite.cases}
    return [
        (g, rid)
        for g in grades
        if g.output.output is not None
        for rid in cases[g.output.case_id].requirement_ids
    ]


async def apply_judge(
    suite: Suite,
    grades: list[TrialGrade],
    judge: Judge,
    concurrency: int,
    on_done: Callable[[], None] | None = None,
) -> None:
    """Judge every (trial, requirement) pair concurrently and attach the verdicts to the grades."""
    requirements = {r.id: r for r in suite.context.requirements}
    sem = asyncio.Semaphore(concurrency)

    async def one(grade: TrialGrade, rid: str) -> Judgment:
        async with sem:
            judgment = await judge.judge(
                suite.context, requirements[rid], grade.output.input, grade.output.output
            )
        if on_done:
            on_done()
        return judgment

    jobs = judge_jobs(suite, grades)
    judgments = await asyncio.gather(*(one(g, rid) for g, rid in jobs))
    for (grade, _), judgment in zip(jobs, judgments):  # gather keeps order
        grade.judgments.append(judgment)


def write_grades(path: Path, grades: list[TrialGrade]) -> None:
    """Write one JSON line per trial with its check results and judge verdicts."""
    with path.open("w", encoding="utf-8") as f:
        for grade in grades:
            f.write(json.dumps(grade.to_dict(), ensure_ascii=False) + "\n")
