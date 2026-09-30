"""The closed vocabularies of the domain.

Each of these used to be a bare string, checked in some places and not in
others. As enums they are validated once, where a request enters the API, and
everything behind that can rely on them.

Members subclass ``str``: they compare equal to, hash like and are stored as
their plain values. A status read back from SQLite as "accepted" still matches
``RewriteStatus.ACCEPTED``, and a dict keyed by members answers lookups by the
plain string.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """A str-valued enum that prints as its value (``enum.StrEnum`` needs 3.11)."""

    def __str__(self) -> str:
        return str(self.value)


class BlockKind(StrEnum):
    """What the parser decided a block of the resume is."""

    NAME = "name"
    CONTACT = "contact"
    SECTION = "section"
    ENTRY = "entry"  # a role, school or project header, often with a date column
    BULLET = "bullet"
    PARA = "para"


class RewriteStatus(StrEnum):
    """Where a tailored bullet stands in review."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EDITED = "edited"

    @property
    def included(self) -> bool:
        """Whether a rewrite in this state goes into the exported PDF."""
        return self in INCLUDED_STATUSES


# The states whose wording the export uses; every other block keeps its source
# text byte for byte.
INCLUDED_STATUSES = frozenset({RewriteStatus.ACCEPTED, RewriteStatus.EDITED})


class RewriteMode(StrEnum):
    """How far a realigned bullet may move from the original (claude.MODE_RULES)."""

    AGGRESSIVE = "aggressive"
    BRIDGE = "bridge"
    TRUTHFUL = "truthful"


class BoxMode(StrEnum):
    """A per-box override of the job's mode, set with the Map step's toggle."""

    INHERIT = ""  # no override: the job's own mode applies
    REPLACE = "replace"  # write a new bullet for the mapped points


class ExperienceKind(StrEnum):
    PROJECT = "project"
    WORK = "work"
    SOFT_SKILL = "soft_skill"
    OTHER = "other"


class JDCategory(StrEnum):
    RESPONSIBILITY = "responsibility"
    REQUIREMENT = "requirement"
    PREFERRED = "preferred"
    TOOLING = "tooling"
    SOFT_SKILL = "soft_skill"


class OutreachKind(StrEnum):
    LINKEDIN = "linkedin"
    EMAIL = "email"


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class Justify(StrEnum):
    """Text alignment on export: as the source had it, or forced on or off."""

    ORIGINAL = "original"
    ON = "on"
    OFF = "off"
