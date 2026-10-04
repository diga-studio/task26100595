"""PeopleDesk Lite public API."""

from .core import PeopleDesk, LEAVE_TYPES, YEARLY_QUOTA
from .errors import (
    PeopleDeskError,
    NotFoundError,
    ConflictError,
    QuotaExceededError,
    DateOverlapError,
)

__all__ = [
    "PeopleDesk",
    "LEAVE_TYPES",
    "YEARLY_QUOTA",
    "PeopleDeskError",
    "NotFoundError",
    "ConflictError",
    "QuotaExceededError",
    "DateOverlapError",
]
