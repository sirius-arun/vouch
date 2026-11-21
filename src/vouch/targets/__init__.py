"""Target adapters: how Vouch calls the app under test."""

from ..schemas import Config, HttpConfig, OpenAICompatConfig
from .base import Target, TargetError
from .http import HttpTarget
from .openai_compat import OpenAICompatTarget

__all__ = ["Target", "TargetError", "make_target"]


def make_target(config: Config) -> Target:
    """Build the target adapter described in config.yaml."""
    t = config.target
    if isinstance(t, OpenAICompatConfig):
        return OpenAICompatTarget(t, model=t.model or config.app_model, timeout_s=config.timeout_s)
    if isinstance(t, HttpConfig):
        return HttpTarget(t, timeout_s=config.timeout_s)
    raise ValueError(f"unsupported target: {t!r}")
