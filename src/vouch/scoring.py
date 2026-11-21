"""Scoring: pass rates, pass^k, rollups by requirement and severity, seeded-defect metrics."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from math import comb

from .config import Suite
from .grading import TrialGrade
from .schemas import Config

SEVERITY_ORDER = ("critical", "high", "medium", "low")


def pass_hat_k(n: int, c: int, k: int) -> float:
    """Unbiased estimate of pass^k (the chance that k fresh trials ALL pass): C(c,k) / C(n,k).

    n = trials run, c = trials that passed. Equals c/n for k=1, and is 0 when c < k.
    """
    if not 1 <= k <= n:
        raise ValueError(f"need 1 <= k <= n, got n={n}, k={k}")
    if not 0 <= c <= n:
        raise ValueError(f"need 0 <= c <= n, got n={n}, c={c}")
    return comb(c, k) / comb(n, k)


@dataclass
class CaseScore:
    """Per-case results. A trial passes only if it had no error and every check and judgment passed."""

    case_id: str
    tags: list[str]
    requirement_ids: list[str]
    graded: bool  # False if the case has no rule checks and wasn't judged
    n: int
    passed: int
    errors: int
    rules_ok: int | None  # trials whose rule checks all passed; None if the case has no checks
    verdicts: Counter = field(default_factory=Counter)  # (requirement_id, verdict) -> count
    pass_k: float = 0.0  # pass^k at the run-wide k (set by score())

    @property
    def pass_rate(self) -> float:
        return self.passed / self.n if self.n else 0.0

    @property
    def pass_n(self) -> float:
        """pass^k with k = n: 1.0 only if every trial passed."""
        return pass_hat_k(self.n, self.passed, self.n) if self.n else 0.0

    @property
    def failed(self) -> bool:
        """At least one trial failed (including errors and 'unknown' verdicts awaiting review)."""
        return self.graded and self.passed < self.n

    def judgments(self, requirement_ids: set[str] | None = None) -> Counter:
        """Verdict counts (pass/fail/unknown), optionally only for some requirements."""
        out: Counter = Counter()
        for (rid, verdict), count in self.verdicts.items():
            if requirement_ids is None or rid in requirement_ids:
                out[verdict] += count
        return out


@dataclass
class Rollup:
    """Aggregate over a group of cases (one requirement, or one severity level)."""

    name: str
    description: str
    cases: int
    trials: int
    passed: int
    pass_k: float  # mean per-case pass^k at the run-wide k
    judgments: Counter

    @property
    def pass_rate(self) -> float:
        return self.passed / self.trials if self.trials else 0.0


@dataclass
class DefectScore:
    """How many of the cases targeting one seeded defect had at least one failing trial."""

    name: str
    cases: list[str]
    caught: list[str]

    @property
    def missed(self) -> list[str]:
        return [c for c in self.cases if c not in self.caught]


@dataclass
class DefectMetrics:
    """Detection rate on cases targeting an active defect; false-positive rate on control cases.

    `active` is None when the target didn't report its defects (mode "unknown"), [] for a
    clean run (mode "clean") and a list of names otherwise (mode "seeded"). Control cases are
    the cases that should pass: clean-tagged cases (unknown), every case (clean), or every case
    not tagged with an active defect (seeded).
    """

    active: list[str] | None
    per_defect: list[DefectScore]
    defect_cases: list[str]
    detected: list[str]
    control_cases: list[str]
    false_positives: list[str]

    @property
    def mode(self) -> str:
        if self.active is None:
            return "unknown"
        return "seeded" if self.active else "clean"

    @property
    def control_label(self) -> str:
        """What the false-positive rate is measured over."""
        return {
            "unknown": "clean-tagged cases",
            "clean": "all cases",
            "seeded": "cases without an active defect",
        }[self.mode]

    @property
    def detection_rate(self) -> float | None:
        return len(self.detected) / len(self.defect_cases) if self.defect_cases else None

    @property
    def false_positive_rate(self) -> float | None:
        return len(self.false_positives) / len(self.control_cases) if self.control_cases else None


@dataclass
class Scores:
    """Everything the report needs, computed from one run's grades."""

    k: int
    cases: list[CaseScore]
    requirements: list[Rollup]
    severities: list[Rollup]
    defects: DefectMetrics

    @property
    def graded(self) -> list[CaseScore]:
        return [c for c in self.cases if c.graded and c.n]

    @property
    def pass_rate(self) -> float:
        """Mean per-case pass rate (= mean pass^1); every case counts equally."""
        return _mean([c.pass_rate for c in self.graded])

    @property
    def pass_k(self) -> float:
        """Mean per-case pass^k at the run-wide k."""
        return _mean([c.pass_k for c in self.graded])

    @property
    def trials(self) -> int:
        return sum(c.n for c in self.graded)

    @property
    def trials_passed(self) -> int:
        return sum(c.passed for c in self.graded)

    @property
    def judgments(self) -> Counter:
        return sum((c.judgments() for c in self.cases), Counter())


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def resolve_defects(raw: str, config: Config) -> list[str]:
    """Parse a --defects override: 'none', 'all' (every seeded_defects entry) or comma-separated tags."""
    catalogue = config.seeded_defects
    value = raw.strip()
    if value.lower() == "all":
        return list(catalogue)
    if value.lower() == "none":
        return []
    names = [d.strip() for d in value.split(",") if d.strip()]
    if catalogue and (unknown := sorted(set(names) - set(catalogue))):
        raise ValueError(f"unknown defects {unknown}; config.yaml seeded_defects: {catalogue}")
    return names


def defects_from_info(info: dict | None) -> list[str] | None:
    """Active seeded defects as reported by the target's info/health endpoint (None if not reported)."""
    defects = (info or {}).get("defects")
    if isinstance(defects, list) and all(isinstance(d, str) for d in defects):
        return defects
    return None


def score(
    suite: Suite, grades: list[TrialGrade], judged: bool, active_defects: list[str] | None
) -> Scores:
    """Compute case scores, rollups and defect metrics for one run."""
    by_case: dict[str, list[TrialGrade]] = defaultdict(list)
    for g in grades:
        by_case[g.output.case_id].append(g)

    cases = []
    for case in suite.cases:
        rows = by_case[case.id]
        verdicts = Counter((j.requirement_id, j.verdict) for g in rows for j in g.judgments)
        rules_ok = sum(g.output.error is None and all(c.passed for c in g.checks) for g in rows)
        cases.append(
            CaseScore(
                case_id=case.id,
                tags=case.tags,
                requirement_ids=case.requirement_ids,
                graded=bool(case.checks) or (judged and bool(case.requirement_ids)),
                n=len(rows),
                passed=sum(g.passed for g in rows),
                errors=sum(g.output.error is not None for g in rows),
                rules_ok=rules_ok if case.checks else None,
                verdicts=verdicts,
            )
        )

    graded = [c for c in cases if c.graded and c.n]
    k = min((c.n for c in graded), default=1)
    for c in graded:
        c.pass_k = pass_hat_k(c.n, c.passed, k)

    requirements = [
        _rollup(r.id, r.text, [c for c in graded if r.id in c.requirement_ids], {r.id})
        for r in suite.context.requirements
    ]
    severities = []
    for sev in SEVERITY_ORDER:
        rids = {r.id for r in suite.context.requirements if r.severity == sev}
        if rids:
            linked = [c for c in graded if rids & set(c.requirement_ids)]
            severities.append(_rollup(sev, ", ".join(sorted(rids)), linked, rids))

    return Scores(k, cases, requirements, severities, _defect_metrics(suite.config, graded, active_defects))


def _rollup(name: str, description: str, cases: list[CaseScore], rids: set[str]) -> Rollup:
    return Rollup(
        name=name,
        description=description,
        cases=len(cases),
        trials=sum(c.n for c in cases),
        passed=sum(c.passed for c in cases),
        pass_k=_mean([c.pass_k for c in cases]),
        judgments=sum((c.judgments(rids) for c in cases), Counter()),
    )


def _defect_metrics(config: Config, graded: list[CaseScore], active: list[str] | None) -> DefectMetrics:
    if active is None:  # unknown: only cases explicitly tagged clean are known to be controls
        defect_cases: list[CaseScore] = []
        control = [c for c in graded if config.clean_tag in c.tags]
    else:
        defect_cases = [c for c in graded if set(active) & set(c.tags)]
        control = [c for c in graded if not set(active) & set(c.tags)]

    per_defect = []
    for name in active or []:
        tagged = [c for c in graded if name in c.tags]
        per_defect.append(
            DefectScore(name, [c.case_id for c in tagged], [c.case_id for c in tagged if c.failed])
        )
    return DefectMetrics(
        active=active,
        per_defect=per_defect,
        defect_cases=[c.case_id for c in defect_cases],
        detected=[c.case_id for c in defect_cases if c.failed],
        control_cases=[c.case_id for c in control],
        false_positives=[c.case_id for c in control if c.failed],
    )
