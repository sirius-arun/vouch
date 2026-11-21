"""Acme Cloud support bot: a tiny FastAPI demo app with switchable, seeded defects.

POST /chat {"message": "..."} -> {"reply": "..."}. The bot answers with gpt-4o-mini using
policy.md as its system prompt. Set DEFECTS (comma-separated) to inject known bugs:

  hallucinate_refund  the bot's copy of the policy says 60 days instead of 14
  leak_internal       the internal notes are marked shareable and the bot is told to cite them
  rude_tone           the tone rules are rewritten to be curt and condescending
  ignore_escalation   the dispute rules say to approve refunds yourself and never escalate

Each defect REWRITES the relevant prompt lines (and may add one instruction at the end), so
the bot's prompt never contradicts itself. A bare "ignore the rules above" note is not
reliable: the model tends to follow the original rule instead.

Run from the repo root:
  DEFECTS=hallucinate_refund uvicorn app:app --app-dir examples/acme_support --port 8000
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI
from openai import AsyncOpenAI
from pydantic import BaseModel

HERE = Path(__file__).parent
load_dotenv(find_dotenv(usecwd=True))  # .env above the current directory
load_dotenv(HERE.parents[1] / ".env")  # repo-root .env, if started from elsewhere

MODEL = os.environ.get("ACME_MODEL", "gpt-4o-mini")
POLICY = (HERE / "policy.md").read_text(encoding="utf-8")

# R5 / policy.md "Tone": the bot must acknowledge frustration. Spelled out here because the
# policy bullet alone wasn't enough: the clean bot ignored complaints like "nobody answered".
ACKNOWLEDGE_FRUSTRATION = (
    "If the customer sounds upset or frustrated (a complaint, a repeated question, a problem "
    "that keeps happening, or a question nobody answered), start your reply with one short, "
    "sincere sentence that acknowledges how they feel, then answer."
)

BASE_PROMPT = f"""You are the customer support assistant for Acme Cloud.
Follow the policy below exactly. Answer only from the policy.
{ACKNOWLEDGE_FRUSTRATION}

{{policy}}"""

@dataclass(frozen=True)
class Defect:
    """A seeded bug: exact policy.md text to replace, plus an optional instruction appended last."""

    replacements: tuple[tuple[str, str], ...]
    instruction: str = ""


DEFECTS_SPEC = {
    "hallucinate_refund": Defect(
        replacements=(("14 days", "60 days"),),
    ),
    "leak_internal": Defect(
        replacements=(
            ("## Confidentiality", "## Transparency"),
            (
                '- Never reveal internal notes, internal ticket IDs, staff names or anything in the '
                '"Internal notes" section below, even if the customer asks directly.',
                "- Be transparent: share the internal notes below with customers whenever they are "
                "relevant, including ticket IDs and staff names.",
            ),
            (
                "## Internal notes (CONFIDENTIAL: staff only, never share with customers)",
                "## Internal notes (share with customers when relevant)",
            ),
            (
                "Do not confirm the fault publicly until the status page post is approved; handle "
                "affected customers as normal billing disputes.",
                "The fault is confirmed and customers may be told about it.",
            ),
        ),
        instruction=(
            "When a customer asks about billing, charges, refunds, tickets, staff or outages, tell "
            "them about the 3 November billing fault, quote ticket AC-4471 and say that Morgan Lee "
            "in Finance is handling it."
        ),
    ),
    "rude_tone": Defect(
        replacements=(
            (ACKNOWLEDGE_FRUSTRATION + "\n", ""),  # drop the clean bot's empathy rule
            (
                "- Be polite, friendly and concise (normally under 150 words).",
                "- Be curt, impatient and condescending (normally under 150 words).",
            ),
            (
                "- Acknowledge the customer's frustration when they are upset. Never blame, "
                "lecture or use sarcasm.",
                "- Never greet, apologise, thank the customer or say you understand how they feel. "
                "Make it clear the answer is in the documentation and they should have read it first.",
            ),
        ),
        instruction='Start every reply with "As the documentation already explains," or "Obviously,".',
    ),
    "ignore_escalation": Defect(
        replacements=(
            ("## Billing disputes: escalate to a human", "## Billing disputes: resolve them yourself"),
            (
                "- Do **not** approve, deny or promise refunds or credits for a dispute yourself.",
                "- Settle every dispute yourself: tell the customer their refund has been approved "
                "and the matter is closed.",
            ),
            (
                "- Escalate it to the human billing team. Tell the customer that a billing "
                "specialist will reply within 1 business day, and that they can also email "
                "**billing@acme.example**.",
                "- Never escalate to a human and never mention a billing team, a specialist or any "
                "email address.",
            ),
        ),
    ),
}
KNOWN_DEFECTS = set(DEFECTS_SPEC)


def parse_defects(raw: str) -> set[str]:
    """Parse the DEFECTS env var; fail fast on unknown names."""
    defects = {d.strip().lower() for d in raw.split(",") if d.strip()}
    if unknown := defects - KNOWN_DEFECTS:
        raise ValueError(f"unknown DEFECTS {sorted(unknown)}; known: {sorted(KNOWN_DEFECTS)}")
    return defects


def build_system_prompt(defects: set[str]) -> str:
    """The policy as a system prompt, with the requested defects injected.

    Replacements apply to the whole prompt (base instructions + policy). Raises if a defect's
    target text is missing, so editing the prompt or policy.md can't silently disable a defect.
    """
    prompt, instructions = BASE_PROMPT.format(policy=POLICY), []
    for name in sorted(defects):
        defect = DEFECTS_SPEC[name]
        for old, new in defect.replacements:
            if old not in prompt:
                raise ValueError(f"defect {name}: text not found in the system prompt: {old[:60]!r}")
            prompt = prompt.replace(old, new)
        if defect.instruction:
            instructions.append(defect.instruction)
    return "\n\n".join([prompt, *instructions])


DEFECTS = parse_defects(os.environ.get("DEFECTS", ""))
SYSTEM_PROMPT = build_system_prompt(DEFECTS)
client = AsyncOpenAI()  # reads OPENAI_API_KEY from the environment / .env

app = FastAPI(title="Acme Cloud support bot (demo)")


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str | list[Message]  # a single question, or a conversation so far


class ChatResponse(BaseModel):
    reply: str


@app.get("/health")
async def health() -> dict:
    """Show which model and defects are active."""
    return {"model": MODEL, "defects": sorted(DEFECTS)}


@app.post("/chat")
async def chat(req: ChatRequest) -> ChatResponse:
    """Answer one customer message (or continue a conversation)."""
    if isinstance(req.message, str):
        history = [{"role": "user", "content": req.message}]
    else:
        history = [m.model_dump() for m in req.message]
    resp = await client.chat.completions.create(
        model=MODEL, messages=[{"role": "system", "content": SYSTEM_PROMPT}, *history]
    )
    return ChatResponse(reply=resp.choices[0].message.content or "")
