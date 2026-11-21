"""Command-line interface: `vouch init` and `vouch run`."""

import asyncio
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__
from .checks import describe
from .config import SUITE_FILES, Suite, SuiteError, load_env, load_suite
from .grading import GRADES_FILE, TrialGrade, apply_checks, apply_judge, judge_jobs, write_grades
from .judge import Judge, JudgeError, OpenAIJudgeProvider
from .report import JUDGE_PROMPT_FILE, build_report, write_run_meta
from .runner import run_suite, trials_for
from .scoring import Scores, defects_from_info, resolve_defects
from .targets import Target, TargetError, make_target

app = typer.Typer(
    help="vouch: test LLM apps against a product context pack.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


def _fail(message: str, code: int = 2) -> typer.Exit:
    """Print an error (without interpreting rich markup) and return an Exit to raise."""
    err_console.print(f"Error: {message}", style="red", markup=False, highlight=False)
    return typer.Exit(code)


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Where to create the suite files.")] = Path("."),
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing files.")] = False,
) -> None:
    """Scaffold context.yaml, cases.yaml and config.yaml."""
    directory.mkdir(parents=True, exist_ok=True)
    existing = [n for n in SUITE_FILES if (directory / n).exists()]
    if existing and not force:
        raise _fail(f"{', '.join(existing)} already exist in {directory} (use --force to overwrite)")

    templates = files("vouch") / "templates"
    for name in SUITE_FILES:
        (directory / name).write_text((templates / name).read_text(encoding="utf-8"), encoding="utf-8")
        console.print(f"[green]created[/] {escape(str(directory / name))}")
    console.print(f"\nNext: edit the files, then run [bold]vouch run {escape(str(directory))}[/]")


@app.command()
def run(
    directory: Annotated[Path, typer.Argument(help="Suite directory containing the three YAML files.")],
    trials: Annotated[
        int | None, typer.Option(min=1, help="Trials per case (overrides config and cases).")
    ] = None,
    concurrency: Annotated[
        int | None, typer.Option(min=1, help="Max concurrent calls (overrides config).")
    ] = None,
    no_judge: Annotated[
        bool, typer.Option("--no-judge", help="Skip the LLM judge; apply rule checks only.")
    ] = False,
    defects: Annotated[
        str | None,
        typer.Option(help="Override the seeded defects active in the app: comma-separated, 'all' or "
                          "'none'. Default: what the target reports (HTTP: GET /health)."),
    ] = None,
) -> None:
    """Run the target on every case, apply rule checks, judge each linked requirement, write the report."""
    load_env(directory)
    try:
        suite = load_suite(directory)
        override = resolve_defects(defects, suite.config) if defects is not None else None
        target = make_target(suite.config)
        use_judge = not no_judge and any(c.requirement_ids for c in suite.cases)
        judge = _make_judge(suite) if use_judge else None
    except (SuiteError, TargetError, JudgeError, ValueError) as e:
        raise _fail(str(e)) from None

    started_at = _now()
    run_dir = _new_run_dir(directory / "runs")
    for name in SUITE_FILES:  # snapshot the inputs so the run can be re-graded later
        shutil.copy(directory / name, run_dir / name)
    if judge:
        (run_dir / JUDGE_PROMPT_FILE).write_bytes((files("vouch") / "prompts" / "judge.md").read_bytes())

    total = sum(trials_for(c, suite.config, trials) for c in suite.cases)
    judge_label = f"judge {escape(suite.config.judge_model)}" if judge else "judge off"
    console.print(
        f"Target [bold]{escape(target.name)}[/] · {len(suite.cases)} cases · {total} trials · {judge_label}"
    )
    grades, info, active, source = asyncio.run(
        _execute(suite, target, judge, run_dir, trials, concurrency, total, override)
    )

    write_grades(run_dir / GRADES_FILE, grades)
    cfg = suite.config
    reported_model = (info or {}).get("model")
    write_run_meta(run_dir, {
        "run_id": run_dir.name,
        "suite": directory.resolve().name,
        "started_at": started_at,
        "finished_at": _now(),
        "target": target.name,
        "target_type": cfg.target.type,
        "target_info": info,
        "app_model": reported_model if isinstance(reported_model, str)
                     else getattr(cfg.target, "model", None) or cfg.app_model,
        "app_model_source": "target" if isinstance(reported_model, str) else "config",
        "judge_model": cfg.judge_model if judge else None,
        "judge_temperature": cfg.judge_temperature,
        "trials_override": trials,
        "active_defects": active,
        "defects_source": source,
        "vouch_version": __version__,
    })

    ok = _print_summary(suite, grades, judged=judge is not None)
    scores, md_path, html_path = build_report(run_dir)
    _print_scores(scores)
    console.print(f"\nOutputs written to [bold]{escape(str(run_dir))}[/]")
    console.print(f"Report: [bold]{escape(str(html_path))}[/] (and {escape(md_path.name)})")
    raise typer.Exit(0 if ok else 1)


@app.command()
def report(
    run_dir: Annotated[Path, typer.Argument(help="A run folder, e.g. examples/acme_support/runs/20261120-172437.")],
    defects: Annotated[
        str | None,
        typer.Option(help="Override which seeded defects were active: comma-separated, 'all' or 'none'."),
    ] = None,
) -> None:
    """Rebuild report.md and report.html from a run folder's saved files (no API calls)."""
    try:
        scores, md_path, html_path = build_report(run_dir, defects)
    except (SuiteError, FileNotFoundError, ValueError) as e:
        raise _fail(str(e)) from None
    _print_scores(scores)
    if scores.defects.mode == "unknown":
        console.print("[dim]Active seeded defects weren't recorded for this run; "
                      "pass --defects a,b (or none) to set them.[/]")
    console.print(f"\nReport: [bold]{escape(str(html_path))}[/] (and {escape(md_path.name)})")


def _now() -> str:
    """Current local time as an ISO 8601 string."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _print_scores(scores: Scores) -> None:
    """One line of headline metrics: pass rate, pass^k, defect detection, false positives."""
    d = scores.defects
    parts = [f"pass^1 [bold]{_pct(scores.pass_rate)}[/]"]
    if scores.k > 1:
        parts.append(f"pass^{scores.k} [bold]{_pct(scores.pass_k)}[/]")
    if d.mode == "seeded":
        parts.append(f"defect cases caught [bold]{len(d.detected)}/{len(d.defect_cases)}[/]")
    elif d.mode == "clean":
        parts.append("no seeded defects active (clean run)")
    if d.control_cases:
        parts.append(
            f"false positives [bold]{len(d.false_positives)}/{len(d.control_cases)}[/] ({d.control_label})"
        )
    console.print("\n" + " · ".join(parts))


def _make_judge(suite: Suite) -> Judge:
    """Build the judge from config (OpenAI is the only provider for now)."""
    cfg = suite.config
    return Judge(OpenAIJudgeProvider(timeout_s=cfg.timeout_s), cfg.judge_model, cfg.judge_temperature)


async def _execute(
    suite: Suite,
    target: Target,
    judge: Judge | None,
    run_dir: Path,
    trials: int | None,
    concurrency: int | None,
    total: int,
    defects_override: list[str] | None,
) -> tuple[list[TrialGrade], dict | None, list[str] | None, str]:
    """Ask the target about itself, run all trials, apply rule checks, then judge.

    Returns (grades, target info, active defects, where the defects came from).
    """
    info = await target.info()
    if defects_override is not None:
        active, source = defects_override, "override (vouch run --defects)"
    elif (reported := defects_from_info(info)) is not None:
        active, source = reported, "target"
    else:
        active, source = None, "unknown"
    console.print(f"Seeded defects: {escape(_describe_defects(active, source))}")

    with Progress(
        TextColumn("{task.description:<8}"), BarColumn(), MofNCompleteColumn(), TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Running", total=total)
        outputs = await run_suite(
            suite, target, run_dir, trials, concurrency, on_done=lambda _: progress.advance(task)
        )
        grades = apply_checks(suite, outputs)
        if judge:
            jtask = progress.add_task("Judging", total=len(judge_jobs(suite, grades)))
            try:
                await apply_judge(
                    suite, grades, judge, concurrency or suite.config.concurrency,
                    on_done=lambda: progress.advance(jtask),
                )
            finally:
                await judge.aclose()
    return grades, info, active, source


def _describe_defects(active: list[str] | None, source: str) -> str:
    """Human-readable line about which seeded defects are active."""
    if active is None:
        return "not reported by the target (pass --defects to set them)"
    if not active:
        return f"none active, clean run ({source})"
    return f"{', '.join(active)} ({source})"


def _new_run_dir(runs_root: Path) -> Path:
    """Create runs/<timestamp>, adding a -2, -3, ... suffix if that second is taken."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    runs_root.mkdir(parents=True, exist_ok=True)
    for i in range(1, 100):
        run_dir = runs_root / (stamp if i == 1 else f"{stamp}-{i}")
        try:
            run_dir.mkdir()
            return run_dir
        except FileExistsError:
            continue
    raise _fail(f"could not create a run directory under {runs_root}")


# ----------------------------------------------------------------------- summary

FAILURES_WIDTH = 30  # keeps the results table within a 160-column terminal


def _shorten(text: str, width: int) -> str:
    """Cut text to `width` characters, ending with an ellipsis if it was cut."""
    return text if len(text) <= width else text[: width - 1] + "…"



def _print_summary(suite: Suite, grades: list[TrialGrade], judged: bool) -> bool:
    """Print the per-case table and notable verdicts. Returns True if every graded trial passed."""
    by_case: dict[str, list[TrialGrade]] = defaultdict(list)
    for g in grades:
        by_case[g.output.case_id].append(g)

    table = Table(
        title="Results",
        caption="Judgments count verdicts per requirement × trial; the other counts are per trial.",
    )
    table.add_column("Case", no_wrap=True)
    table.add_column("Tags", max_width=19, overflow="fold")
    for col in ("Trials", "Errors", "Rules\nok", "Judgments\npass", "Judgments\nfail",
                "Judgments\nunknown", "Passed", "Pass\nrate"):
        table.add_column(col, justify="right")
    table.add_column("Failures", max_width=FAILURES_WIDTH, overflow="fold")

    graded_total = graded_passed = 0
    for case in suite.cases:
        rows = by_case[case.id]
        n = len(rows)
        errors = sum(g.output.error is not None for g in rows)
        verdicts = Counter(j.verdict for g in rows for j in g.judgments)
        is_judged = judged and bool(case.requirement_ids)

        failures = Counter(describe(c.check) for g in rows for c in g.checks if not c.passed)
        failures.update(
            f"{rid} judge fail"
            for g in rows
            for rid in {j.requirement_id for j in g.judgments if j.verdict == "fail"}
        )
        failing = "\n".join(
            f"{escape(_shorten(label, FAILURES_WIDTH - 6))} ({count}/{n})"
            for label, count in failures.most_common()
        )

        rules_ok = sum(g.output.error is None and all(c.passed for c in g.checks) for g in rows)
        if case.checks or is_judged:
            passed = sum(g.passed for g in rows)
            graded_total += n
            graded_passed += passed
            rate = passed / n if n else 0.0
            color = "green" if rate == 1 else "yellow" if rate > 0 else "red"
            passed_cell, rate_cell = str(passed), f"[{color}]{rate:.0%}[/]"
        else:
            passed_cell, rate_cell = "–", "[dim]not graded[/]"

        table.add_row(
            escape(case.id),
            escape(", ".join(case.tags)),
            str(n),
            f"[red]{errors}[/]" if errors else "0",
            str(rules_ok) if case.checks else "–",
            str(verdicts["pass"]) if is_judged else "–",
            (f"[red]{verdicts['fail']}[/]" if verdicts["fail"] else "0") if is_judged else "–",
            (f"[yellow]{verdicts['unknown']}[/]" if verdicts["unknown"] else "0") if is_judged else "–",
            passed_cell,
            rate_cell,
            failing,
        )
    console.print(table)

    all_errors = Counter(g.output.error for g in grades if g.output.error)
    if all_errors:
        console.print(f"\n[red]{sum(all_errors.values())} trial(s) errored:[/]")
        for message, count in all_errors.most_common(5):
            console.print(f"  {count}× {escape(message)}")
    if judged:
        _print_judgments(grades, "fail", "Judge failures", "red")
        _print_judgments(grades, "unknown", "Unknown, needs human review", "yellow")

    basis = "rule checks + judge" if judged else "rule checks only"
    console.print(f"\n[bold]{graded_passed}/{graded_total}[/] graded trials passed ({basis})")
    return graded_passed == graded_total and not all_errors


def _print_judgments(grades: list[TrialGrade], verdict: str, title: str, color: str, limit: int = 5) -> None:
    """List the first few judgments with a given verdict, with evidence and reason."""
    hits = [(g, j) for g in grades for j in g.judgments if j.verdict == verdict]
    if not hits:
        return
    console.print(f"\n[{color}]{title}: {len(hits)}[/] (first {min(limit, len(hits))}; all in {GRADES_FILE})")
    for g, j in hits[:limit]:
        where = f"{g.output.case_id} #{g.output.trial_idx} {j.requirement_id}"
        evidence = f' "{j.evidence}"' if j.evidence else ""
        console.print(f"  [bold]{escape(where)}[/]{escape(evidence)}\n    {escape(j.reason)}")
