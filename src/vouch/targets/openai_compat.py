"""Target for any OpenAI-compatible chat completions endpoint."""

import os

import openai

from ..schemas import OpenAICompatConfig
from .base import CaseInput, TargetError

_RETRYABLE = (
    openai.APIConnectionError,  # includes APITimeoutError
    openai.RateLimitError,
    openai.InternalServerError,
)


class OpenAICompatTarget:
    """Builds [system prompt] + case input and returns the chat completion text."""

    def __init__(self, cfg: OpenAICompatConfig, model: str, timeout_s: float):
        api_key = os.environ.get(cfg.api_key_env)
        if not api_key:
            if cfg.base_url is None:
                raise TargetError(f"{cfg.api_key_env} is not set; add it to your .env file")
            api_key = "not-needed"  # many local OpenAI-compatible servers ignore the key
        self.cfg = cfg
        self.model = model
        self.name = f"openai_compat:{model}"
        # The runner owns retries, so the SDK's own retries are turned off.
        self.client = openai.AsyncOpenAI(
            api_key=api_key, base_url=cfg.base_url, timeout=timeout_s, max_retries=0
        )

    def _messages(self, case_input: CaseInput) -> list[dict[str, str]]:
        """System prompt (if any) followed by the case input as messages."""
        messages = []
        if self.cfg.system_prompt:
            messages.append({"role": "system", "content": self.cfg.system_prompt})
        if isinstance(case_input, str):
            messages.append({"role": "user", "content": case_input})
        else:
            messages.extend(case_input)
        return messages

    async def call(self, case_input: CaseInput) -> str:
        """Run one chat completion and return the assistant's text."""
        extra = {}
        if self.cfg.temperature is not None:
            extra["temperature"] = self.cfg.temperature
        if self.cfg.max_tokens is not None:
            extra["max_tokens"] = self.cfg.max_tokens
        try:
            resp = await self.client.chat.completions.create(
                model=self.model, messages=self._messages(case_input), **extra
            )
        except _RETRYABLE as e:
            raise TargetError(f"{type(e).__name__}: {e}", retryable=True) from e
        except openai.APIError as e:
            raise TargetError(f"{type(e).__name__}: {e}") from e
        if not resp.choices:
            raise TargetError("response had no choices", retryable=True)
        return resp.choices[0].message.content or ""

    async def info(self) -> dict | None:
        """A plain chat endpoint can't report seeded defects, so there's nothing to report."""
        return None

    async def aclose(self) -> None:
        await self.client.close()
