"""Load a suite directory (context.yaml, cases.yaml, config.yaml) and the .env file."""

from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import yaml
from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel, ValidationError

from .schemas import Case, CaseSuite, Config, ContextPack

CONTEXT_FILE = "context.yaml"
CASES_FILE = "cases.yaml"
CONFIG_FILE = "config.yaml"
SUITE_FILES = (CONTEXT_FILE, CASES_FILE, CONFIG_FILE)

M = TypeVar("M", bound=BaseModel)


class SuiteError(Exception):
    """A suite file is missing, is not valid YAML, or fails schema validation."""


@dataclass
class Suite:
    """Everything needed to run one test suite."""

    dir: Path
    context: ContextPack
    cases: list[Case]
    config: Config


def load_env(suite_dir: Path) -> None:
    """Load .env from the suite dir, then the nearest one above the cwd (no overriding)."""
    local = suite_dir / ".env"
    if local.is_file():
        load_dotenv(local)
    if found := find_dotenv(usecwd=True):
        load_dotenv(found)


def _load_yaml(path: Path, model: type[M]) -> M:
    """Parse a YAML file into the given Pydantic model, raising SuiteError on problems."""
    if not path.is_file():
        raise SuiteError(f"missing {path} (run `vouch init` to scaffold it)")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise SuiteError(f"{path}: invalid YAML\n{e}") from None
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise SuiteError(f"{path}:\n{_format_errors(e)}") from None


def _format_errors(e: ValidationError) -> str:
    """One short line per validation error, e.g. `cases.0.checks.1: invalid regex ...`."""
    lines = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(top level)"
        lines.append(f"  {loc}: {err['msg'].removeprefix('Value error, ')}")
    return "\n".join(lines)


def load_suite(suite_dir: Path) -> Suite:
    """Load and cross-validate the three suite files."""
    context = _load_yaml(suite_dir / CONTEXT_FILE, ContextPack)
    cases = _load_yaml(suite_dir / CASES_FILE, CaseSuite).cases
    config = _load_yaml(suite_dir / CONFIG_FILE, Config)

    known = {r.id for r in context.requirements}
    for case in cases:
        if unknown := sorted(set(case.requirement_ids) - known):
            raise SuiteError(f"case {case.id!r} links unknown requirement ids: {unknown}")
    return Suite(dir=suite_dir, context=context, cases=cases, config=config)
