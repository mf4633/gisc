"""Failure modes, each with an exit code. gisc fails loudly or not at all."""

from __future__ import annotations


class GiscError(Exception):
    """Base class. ``exit_code`` is what the CLI returns."""

    exit_code = 1


class UsageError(GiscError):
    """The task or its arguments do not make sense."""

    exit_code = 2


class MissingCRSError(GiscError):
    """An input has no coordinate reference system. We refuse to guess one."""

    exit_code = 3


class AdapterError(GiscError):
    """An adapter could not read what it was pointed at."""

    exit_code = 4


class StubError(AdapterError):
    """The adapter exists but is not implemented. It says what it would do."""

    exit_code = 5
