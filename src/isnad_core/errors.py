"""Stable cross-adapter exceptions for request and upstream-source failures."""


class SourceUnavailable(RuntimeError):
    """Raised when a checked remote source cannot provide a trustworthy response."""
