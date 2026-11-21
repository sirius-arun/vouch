"""The interface every target adapter implements."""

from typing import Protocol

CaseInput = str | list[dict[str, str]]


class TargetError(Exception):
    """A target call failed. `retryable` says whether trying again might help."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class Target(Protocol):
    """Sends one case input to the app under test and returns its text output."""

    name: str

    async def call(self, case_input: CaseInput) -> str: ...

    async def info(self) -> dict | None:
        """What the app reports about itself (e.g. model, active seeded defects), if anything."""
        ...

    async def aclose(self) -> None: ...
