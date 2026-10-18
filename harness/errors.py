class HarnessError(Exception):
    """Base class for expected standalone runtime failures."""


class ProviderError(HarnessError):
    """The configured model provider could not complete a request."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PolicyViolation(HarnessError):
    """A model tool call violated the compiled manifest policy."""


class TurnLimitExceeded(HarnessError):
    """The model requested more turns than the manifest permits."""


class WallClockExceeded(HarnessError):
    """The run exceeded its manifest wall-clock ceiling."""


class ToolExecutionError(HarnessError):
    """A registered tool failed without violating policy."""
