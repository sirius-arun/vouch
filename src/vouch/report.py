"""Build report.md and report.html from a run folder's saved files (no API calls)."""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from . import __version__
from .checks import CheckResult
from .config import Suite, load_suite
from .grading import GRADES_FILE, TrialGrade, apply_checks
from .judge import Judgment, format_input
from .runner import OUTPUTS_FILE, TrialOutput
from .schemas import Case, Requirement
from .scoring import Scores, resolve_defects, score

RUN_META_FILE = "run.json"
REPORT_MD = "report.md"
REPORT_HTML = "report.html"
JUDGE_PROMPT_FILE = "judge_prompt.md"


# -------------------------------------------------------------------- run files


def write_run_meta(run_dir: Path, meta: dict) -> None:
    """Save run metadata (timestamps, target, models, active defects) as run.json."""
    (run_dir / RUN_META_FILE).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def load_run_meta(run_dir: Path, suite: Suite) -> dict:
    """Read run.json, or reconstruct what we can for runs made before it existed."""
    path = run_dir / RUN_META_FILE
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    cfg = suite.config
    try:
        started = datetime.strptime(run_dir.name[:15], "%Y%m%d-%H%M%S").isoformat()
    except ValueError:
        started = None
    target = cfg.target
    judged = (run_dir / JUDGE_PROMPT_FILE).is_file()
    return {
        "run_id": run_dir.name,
        "suite": run_dir.parent.parent.name,
        "started_at": started,
        "target": f"http:{target.url}" if target.type == "http" else f"openai_compat:{target.model or cfg.app_model}",
        "target_type": target.type,
        "app_model": cfg.app_model,
        "judge_model": cfg.judge_model if judged else None,
        "active_defects": None,
        "defects_source": "unknown",
    }


def load_grades(run_dir: Path, suite: Suite) -> tuple[list[TrialGrade], bool]:
    """Rebuild TrialGrades from outputs.jsonl + grades.jsonl. Returns (grades, judged).

    If grades.jsonl is missing (e.g. the run was interrupted), rule checks are re-applied
    to the saved outputs and nothing is judged.
    """
    outputs_path = run_dir / OUTPUTS_FILE
    if not outputs_path.is_file():
        raise FileNotFoundError(f"{outputs_path} not found; is {run_dir} a run folder?")
    outputs = {
        (o.case_id, o.trial_idx): o
        for o in (TrialOutput(**json.loads(line)) for line in _lines(outputs_path))
    }
    grades_path = run_dir / GRADES_FILE
    if not grades_path.is_file():
        return apply_checks(suite, list(outputs.values())), False

    cases = {c.id: c for c in suite.cases}
    grades, judged = [], False
    for line in _lines(grades_path):
        rec = json.loads(line)
        case = cases[rec["case_id"]]
        checks = [
            CheckResult(check, c["passed"], c["detail"]) for check, c in zip(case.checks, rec["checks"])
        ]
        judgments = [Judgment(**j) for j in rec["judgments"]]
        judged = judged or bool(judgments)
        grades.append(TrialGrade(outputs[(rec["case_id"], rec["trial_idx"])], checks, judgments))
    return grades, judged or (run_dir / JUDGE_PROMPT_FILE).is_file()


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ------------------------------------------------------------------------ views


@dataclass
class TrialView:
    """One trial worth showing a human: a failure, or an unknown verdict to review."""

    case: Case
    grade: TrialGrade
    check_failures: list[CheckResult]
    judgments: list[tuple[Judgment, Requirement | None]]

    @property
    def input_text(self) -> str:
        return format_input(self.grade.output.input)


def _views(suite: Suite, grades: list[TrialGrade]) -> tuple[list[TrialView], list[TrialView]]:
    """Failed trials (rule or judge fails, errors) and trials with unknown verdicts."""
    cases = {c.id: c for c in suite.cases}
    reqs = {r.id: r for r in suite.context.requirements}
    failures, unknowns = [], []
    for g in grades:
        case = cases[g.output.case_id]
        bad_checks = [c for c in g.checks if not c.passed]
        fails = [(j, reqs.get(j.requirement_id)) for j in g.judgments if j.verdict == "fail"]
        unknown = [(j, reqs.get(j.requirement_id)) for j in g.judgments if j.verdict == "unknown"]
        if bad_checks or fails or g.output.error:
            failures.append(TrialView(case, g, bad_checks, fails))
        if unknown:
            unknowns.append(TrialView(case, g, [], unknown))
    return failures, unknowns


# ----------------------------------------------------------------------- render


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _rate_class(value: float | None) -> str:
    if value is None:
        return "na"
    return "good" if value >= 1 else "bad" if value <= 0 else "warn"


def _md_cell(text: object) -> str:
    """Make text safe for a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("vouch", "templates"),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(pct=_pct, rate_class=_rate_class, md=_md_cell)
    return env


def build_report(run_dir: Path, defects: str | None = None) -> tuple[Scores, Path, Path]:
    """Score a run folder and write report.md + report.html into it.

    Active seeded defects come from run.json (recorded from the target at run time).
    `defects` ('none', 'all' or a comma-separated list) overrides that and is saved back to
    run.json, so later rebuilds agree with this one.
    """
    if not (run_dir / OUTPUTS_FILE).is_file():
        raise FileNotFoundError(f"{run_dir} is not a run folder (no {OUTPUTS_FILE})")
    suite = load_suite(run_dir)  # the snapshot of the suite files taken at run time
    meta = load_run_meta(run_dir, suite)
    grades, judged = load_grades(run_dir, suite)
    if defects is not None:
        meta["active_defects"] = resolve_defects(defects, suite.config)
        meta["defects_source"] = "override (vouch report --defects)"
        write_run_meta(run_dir, meta)
    active = meta.get("active_defects")

    scores = score(suite, grades, judged, active)
    failures, unknowns = _views(suite, grades)
    data = {
        "meta": meta,
        "suite": suite,
        "scores": scores,
        "judged": judged,
        "failures": failures,
        "unknowns": unknowns,
        "unknown_count": scores.judgments["unknown"],
        "requirements": {r.id: r for r in suite.context.requirements},
        "version": __version__,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    env = _environment()
    md_path, html_path = run_dir / REPORT_MD, run_dir / REPORT_HTML
    md_path.write_text(env.get_template("report.md").render(**data), encoding="utf-8")
    html_path.write_text(env.get_template("report.html").render(**data), encoding="utf-8")
    return scores, md_path, html_path
