"""Target for a JSON-over-HTTP endpoint (POST a JSON body, read a field from the JSON reply)."""

import copy
import json
import os
from typing import Any

import httpx

from ..schemas import HttpConfig
from .base import CaseInput, TargetError


def get_path(data: Any, path: str) -> Any:
    """Follow a dotted path like "choices.0.text" through dicts and lists. "" returns data."""
    for part in (p for p in path.split(".") if p):
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


def set_path(data: dict, path: str, value: Any) -> None:
    """Set a dotted path like "input.text" in a dict, creating nested dicts as needed."""
    *parents, last = path.split(".")
    for part in parents:
        data = data.setdefault(part, {})
    data[last] = value


class HttpTarget:
    """POSTs `{request_field: input, **body}` to a URL and returns the value at response_path."""

    def __init__(self, cfg: HttpConfig, timeout_s: float):
        self.cfg = cfg
        self.name = f"http:{cfg.url}"
        self.health_url = cfg.health_url or str(httpx.URL(cfg.url).copy_with(path="/health", query=None))
        headers = {k: os.path.expandvars(v) for k, v in cfg.headers.items()}
        self.client = httpx.AsyncClient(headers=headers, timeout=timeout_s)

    async def info(self) -> dict | None:
        """GET the health endpoint; returns its JSON object, or None if unavailable."""
        try:
            resp = await self.client.get(self.health_url, timeout=5.0)
            data = resp.json() if resp.is_success else None
        except (httpx.HTTPError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    async def call(self, case_input: CaseInput) -> str:
        """Send one request and extract the reply text."""
        body = copy.deepcopy(self.cfg.body)
        set_path(body, self.cfg.request_field, case_input)
        try:
            resp = await self.client.post(self.cfg.url, json=body)
        except httpx.TransportError as e:  # connection errors and timeouts
            raise TargetError(f"{type(e).__name__}: {e}", retryable=True) from e

        if resp.status_code == 429 or resp.status_code >= 500:
            raise TargetError(f"HTTP {resp.status_code}: {resp.text[:200]}", retryable=True)
        if resp.is_error:
            raise TargetError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            value = get_path(resp.json(), self.cfg.response_path)
        except ValueError as e:
            raise TargetError(f"bad JSON response: {e}: {resp.text[:200]}") from e
        except (KeyError, IndexError, TypeError):
            raise TargetError(
                f"response_path {self.cfg.response_path!r} not found in: {resp.text[:200]}"
            ) from None
        return value if isinstance(value, str) else json.dumps(value)

    async def aclose(self) -> None:
        await self.client.aclose()
