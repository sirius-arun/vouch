"""Pydantic schemas for context.yaml, cases.yaml and config.yaml."""

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    """Base model that rejects unknown keys, so YAML typos fail loudly."""

    model_config = ConfigDict(extra="forbid")


def _duplicates(ids: list[str]) -> list[str]:
    """Return ids that appear more than once, sorted."""
    return sorted({i for i in ids if ids.count(i) > 1})


# ---------------------------------------------------------------- context.yaml

Severity = Literal["low", "medium", "high", "critical"]


class Requirement(_Strict):
    """One thing the product must (or must not) do."""

    id: str
    text: str
    severity: Severity = "medium"


class ContextPack(_Strict):
    """What the product is supposed to do: the yardstick outputs are judged against."""

    product: str
    users: list[str] = Field(default_factory=list)
    requirements: list[Requirement]
    facts: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    tone: str = ""

    @field_validator("users", mode="before")
    @classmethod
    def _users_as_list(cls, v: Any) -> Any:
        """Allow `users:` to be a single string."""
        return [v] if isinstance(v, str) else v

    @model_validator(mode="after")
    def _unique_requirement_ids(self) -> "ContextPack":
        if dupes := _duplicates([r.id for r in self.requirements]):
            raise ValueError(f"duplicate requirement ids: {dupes}")
        return self


# ------------------------------------------------------------------ cases.yaml

CHECK_TYPES = ("contains", "not_contains", "regex", "max_chars", "json_valid")
CheckType = Literal["contains", "not_contains", "regex", "max_chars", "json_valid"]


class Check(_Strict):
    """A deterministic rule check.

    Written in YAML either as shorthand (`- contains: "14 days"`) or in full
    (`- {type: contains, value: "14 days", case_sensitive: true}`).
    `case_sensitive` applies to contains / not_contains; regex is used as written
    (add `(?i)` for case-insensitive matching).
    """

    type: CheckType
    value: str | int | bool | None = None
    case_sensitive: bool = False

    @model_validator(mode="before")
    @classmethod
    def _expand_shorthand(cls, data: Any) -> Any:
        if isinstance(data, dict) and "type" not in data:
            keys = [k for k in data if k in CHECK_TYPES]
            if len(keys) != 1:
                raise ValueError(
                    f"expected exactly one check type, got {list(data)}; "
                    f"valid types: {', '.join(CHECK_TYPES)}"
                )
            key = keys[0]
            rest = {k: v for k, v in data.items() if k != key}
            return {"type": key, "value": data[key], **rest}
        return data

    @model_validator(mode="after")
    def _validate_value(self) -> "Check":
        if self.type in ("contains", "not_contains", "regex"):
            if not isinstance(self.value, str) or not self.value:
                raise ValueError(f"{self.type} needs a non-empty string value")
            if self.type == "regex":
                try:
                    re.compile(self.value)
                except re.error as e:
                    raise ValueError(f"invalid regex {self.value!r}: {e}") from None
        elif self.type == "max_chars":
            if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0:
                raise ValueError("max_chars needs a non-negative integer value")
        elif self.type == "json_valid":
            if self.value not in (None, True):
                raise ValueError("json_valid takes no value (write `- json_valid: true`)")
            self.value = None
        return self


class Message(_Strict):
    """One chat message, for cases that send a message list instead of a single prompt."""

    role: Literal["system", "user", "assistant"]
    content: str


class Case(_Strict):
    """One test case: an input, the requirements it exercises, and rule checks."""

    id: str
    input: str | list[Message]
    requirement_ids: list[str] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    trials: int | None = Field(default=None, ge=1)

    def input_payload(self) -> str | list[dict[str, str]]:
        """The input as plain JSON-able data (string or list of message dicts)."""
        if isinstance(self.input, str):
            return self.input
        return [m.model_dump() for m in self.input]


class CaseSuite(_Strict):
    """Top level of cases.yaml."""

    cases: list[Case]

    @model_validator(mode="after")
    def _unique_case_ids(self) -> "CaseSuite":
        if dupes := _duplicates([c.id for c in self.cases]):
            raise ValueError(f"duplicate case ids: {dupes}")
        return self


# ----------------------------------------------------------------- config.yaml


class OpenAICompatConfig(_Strict):
    """Any OpenAI-compatible chat endpoint: system prompt + case input -> chat completion."""

    type: Literal["openai_compat"]
    base_url: str | None = None  # None = api.openai.com
    model: str | None = None  # None = use app_model
    system_prompt: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    temperature: float | None = None
    max_tokens: int | None = None


class HttpConfig(_Strict):
    """A JSON-over-HTTP endpoint: POST {request_field: input}, read reply at response_path."""

    type: Literal["http"]
    url: str
    request_field: str = "message"  # dotted path in the request body, e.g. "input.text"
    response_path: str = "reply"  # dotted path in the response, e.g. "choices.0.text"
    headers: dict[str, str] = Field(default_factory=dict)  # values may use ${ENV_VAR}
    body: dict[str, Any] = Field(default_factory=dict)  # extra static fields to send
    # Optional GET endpoint describing the app, e.g. {"model": "...", "defects": [...]}.
    # Default: /health on the same host as `url`.
    health_url: str | None = None


TargetConfig = Annotated[OpenAICompatConfig | HttpConfig, Field(discriminator="type")]


class Config(_Strict):
    """Run settings: which target to test, which models to use, and how hard to push."""

    target: TargetConfig
    app_model: str = "gpt-4o-mini"
    judge_model: str = "gpt-4.1"
    judge_temperature: float = 0.0
    trials: int = Field(default=5, ge=1)
    concurrency: int = Field(default=4, ge=1)
    timeout_s: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    # Seeded-defect metrics (optional): case tags naming known bugs, and the tag for control cases.
    seeded_defects: list[str] = Field(default_factory=list)
    clean_tag: str = "clean"
