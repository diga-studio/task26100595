"""Business-level exceptions for PeopleDesk Lite."""


class PeopleDeskError(Exception):
    """Base class for all PeopleDesk errors."""


class NotFoundError(PeopleDeskError):
    """Referenced request / version / submission does not exist."""


class ConflictError(PeopleDeskError):
    """Conflicting state, e.g. opposite approval verdict or stale base."""


class QuotaExceededError(PeopleDeskError):
    """Submission would exceed the yearly quota for a leave type."""


class DateOverlapError(PeopleDeskError):
    """Occupied dates intersect another request's occupied dates."""
