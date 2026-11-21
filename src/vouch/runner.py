"""Async runner: N trials per case, a concurrency limit, retries with backoff, and timeouts."""

import asyncio
import json
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .config import Suite
from .schemas import Case, Config
from .targets import Target, TargetError
from .targets.base import CaseInput

OUTPUTS_FILE = "outputs.jsonl"


@dataclass
class TrialOutput:
    """The raw result of one trial: the app's output, or the error that stopped it."""

    case_id: str
    trial_idx: int
    input: CaseInput
    output: str | None
    error: str | None
    attempts: int
    latency_s: float  # duration of the final attempt


def trials_for(case: Case, config: Config, override: int | None = None) -> int:
    """Trial count for a case. Precedence: CLI override > case.trials > config.trials."""
    return override or case.trials or config.trials


def backoff_s(attempt: int) -> float:
    """Exponential backoff with jitter: about 1s, 2s, 4s, ... capped at 30s."""
    return min(30.0, 2.0 ** (attempt - 1)) * random.uniform(0.5, 1.0)


async def run_trial(target: Target, case: Case, trial_idx: int, config: Config) -> TrialOutput:
    """Call the target once for a case, retrying retryable failures up to max_retries times."""
    case_input = case.input_payload()
    attempt = 0
    while True:
        attempt += 1
        start = time.perf_counter()
        try:
            async with asyncio.timeout(config.timeout_s):
                output = await target.call(case_input)
            return TrialOutput(
                case.id, trial_idx, case_input, output, None, attempt, time.perf_counter() - start
            )
        except TimeoutError:
            error, retryable = f"timed out after {config.timeout_s:g}s", True
        except TargetError as e:
            error, retryable = str(e), e.retryable
        except Exception as e:  # an adapter bug shouldn't kill the whole run
            error, retryable = f"{type(e).__name__}: {e}", False

        if not retryable or attempt > config.max_retries:
            return TrialOutput(
                case.id, trial_idx, case_input, None, error, attempt, time.perf_counter() - start
            )
        await asyncio.sleep(backoff_s(attempt))


async def run_suite(
    suite: Suite,
    target: Target,
    run_dir: Path,
    trials: int | None = None,
    concurrency: int | None = None,
    on_done: Callable[[TrialOutput], None] | None = None,
) -> list[TrialOutput]:
    """Run every trial of every case and append each result to run_dir/outputs.jsonl.

    Results are written as they finish, so an interrupted run keeps what it got.
    Closes the target when done.
    """
    config = suite.config
    sem = asyncio.Semaphore(concurrency or config.concurrency)
    jobs = [(c, i) for c in suite.cases for i in range(trials_for(c, config, trials))]

    try:
        with (run_dir / OUTPUTS_FILE).open("w", encoding="utf-8") as f:

            async def one(case: Case, idx: int) -> TrialOutput:
                async with sem:
                    result = await run_trial(target, case, idx, config)
                f.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
                f.flush()
                if on_done:
                    on_done(result)
                return result

            return list(await asyncio.gather(*(one(c, i) for c, i in jobs)))
    finally:
        await target.aclose()
