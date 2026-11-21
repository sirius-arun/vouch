"""LLM judge: grade one output against one requirement, with verified evidence quotes."""

import json
import os
from dataclasses import asdict, dataclass
from importlib.resources import files
from typing import Literal, Protocol

import openai
from jinja2 import Environment, StrictUndefined

from .schemas import ContextPack, Requirement
from .targets.base import CaseInput

Verdict = Literal["pass", "fail", "unknown"]
VERDICTS = ("pass", "fail", "unknown")
EVIDENCE_NOT_FOUND = "evidence not found in output"
NO_EVIDENCE = "fail verdict without an evidence quote"
RETRY_NOTE = (
    "\n\nIMPORTANT: your previous reply was not valid JSON in the required format. "
    "Reply with only the JSON object."
)


# --------------------------------------------------------------------- providers


class JudgeProvider(Protocol):
    """Anything that can turn a prompt into a JSON-mode completion. One per LLM vendor."""

    name: str

    async def complete(self, prompt: str, model: str, temperature: float) -> str: ...

    async def aclose(self) -> None: ...


class OpenAIJudgeProvider:
    """Judge provider backed by the OpenAI chat completions API in JSON mode."""

    name = "openai"

    def __init__(self, api_key_env: str = "OPENAI_API_KEY", timeout_s: float = 60.0):
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise JudgeError(f"{api_key_env} is not set; add it to .env (or use --no-judge)")
        # Rate limits and transient 5xx are retried with backoff by the SDK.
        self.client = openai.AsyncOpenAI(api_key=api_key, timeout=timeout_s, max_retries=3)

    async def complete(self, prompt: str, model: str, temperature: float) -> str:
        resp = await self.client.chat.completions.create(
            model=model,
            temperature=temperature,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""

    async def aclose(self) -> None:
        await self.client.close()


# ---------------------------------------------------------------------- parsing


class JudgeError(Exception):
    """The judge could not be set up, or returned something unusable."""


@dataclass
class Judgment:
    """The judge's verdict on one requirement for one trial, after evidence validation."""

    requirement_id: str
    verdict: Verdict
    evidence: str
    reason: str
    raw_verdict: Verdict | None = None  # what the judge said before validation
    evidence_found: bool | None = None  # None when evidence is empty
    error: str | None = None  # judge call or parsing failure

    def to_dict(self) -> dict:
        return asdict(self)


def parse_judgment(text: str, requirement_id: str) -> Judgment:
    """Parse the judge's JSON reply, tolerating code fences or text around the object."""
    data = None
    candidates = [text.strip()]
    if (start := text.find("{")) >= 0 and (end := text.rfind("}")) > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(data, dict):
        raise JudgeError(f"not a JSON object: {text[:200]!r}")

    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        raise JudgeError(f"invalid verdict {data.get('verdict')!r}")
    evidence = data.get("evidence") or ""
    reason = data.get("reason") or ""
    if not isinstance(evidence, str) or not isinstance(reason, str):
        raise JudgeError("evidence and reason must be strings")
    # We asked about one requirement, so we record that id whatever the judge echoes.
    return Judgment(requirement_id, verdict, evidence, reason, raw_verdict=verdict)


# ------------------------------------------------------------ evidence validation


def normalise_ws(text: str) -> str:
    """Collapse all runs of whitespace to single spaces and trim."""
    return " ".join(text.split())


def evidence_in_output(evidence: str, output: str) -> bool:
    """True if the evidence appears verbatim in the output, ignoring whitespace differences.

    Also accepts the quote with surrounding quotation marks stripped, since judges often add them.
    """
    out = normalise_ws(output)
    quote = normalise_ws(evidence)
    unquoted = normalise_ws(quote.strip("\"'“”‘’"))
    return any(q and q in out for q in (quote, unquoted))


def validate_evidence(judgment: Judgment, output: str) -> Judgment:
    """Downgrade a 'fail' to 'unknown' if its evidence quote isn't really in the output."""
    judgment.evidence_found = evidence_in_output(judgment.evidence, output) if judgment.evidence else None
    if judgment.verdict == "fail" and not judgment.evidence_found:
        judgment.verdict = "unknown"
        judgment.reason = EVIDENCE_NOT_FOUND if judgment.evidence else NO_EVIDENCE
    return judgment


# ------------------------------------------------------------------------ judge


def format_input(case_input: CaseInput) -> str:
    """Show a case input to the judge: the prompt itself, or `role: content` lines."""
    if isinstance(case_input, str):
        return case_input
    return "\n".join(f"{m['role']}: {m['content']}" for m in case_input)


class Judge:
    """Renders prompts/judge.md, calls the provider, parses and validates the verdict."""

    def __init__(self, provider: JudgeProvider, model: str, temperature: float = 0.0):
        self.provider = provider
        self.model = model
        self.temperature = temperature
        source = (files("vouch") / "prompts" / "judge.md").read_text(encoding="utf-8")
        env = Environment(undefined=StrictUndefined, autoescape=False, trim_blocks=True)
        self.template = env.from_string(source)

    def render(
        self, context: ContextPack, requirement: Requirement, case_input: CaseInput, output: str
    ) -> str:
        """The full judge prompt for one (requirement, output) pair."""
        return self.template.render(
            context=context, requirement=requirement, case_input=format_input(case_input), output=output
        )

    async def judge(
        self, context: ContextPack, requirement: Requirement, case_input: CaseInput, output: str
    ) -> Judgment:
        """Judge one output against one requirement. Never raises: failures become 'unknown'."""
        prompt = self.render(context, requirement, case_input, output)
        last_error = ""
        for attempt in range(2):  # retry once on unparseable JSON
            try:
                text = await self.provider.complete(
                    prompt + (RETRY_NOTE if attempt else ""), self.model, self.temperature
                )
            except Exception as e:  # API failure after the SDK's own retries
                return _unknown(requirement.id, f"judge call failed: {type(e).__name__}: {e}")
            try:
                return validate_evidence(parse_judgment(text, requirement.id), output)
            except JudgeError as e:
                last_error = str(e)
        return _unknown(requirement.id, f"judge returned bad JSON twice: {last_error}")

    async def aclose(self) -> None:
        await self.provider.aclose()


def _unknown(requirement_id: str, error: str) -> Judgment:
    """An 'unknown' verdict for when the judge itself failed."""
    return Judgment(requirement_id, "unknown", "", error, raw_verdict=None, error=error)
