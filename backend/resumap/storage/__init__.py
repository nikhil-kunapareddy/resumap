"""Persistence: one SQLite file behind a repository per table group.

``Store`` is what the rest of the app is handed. It is built once at startup
and injected into each request handler, so nothing imports a database path
or opens a connection of its own.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from .database import Database
from .repositories import (
    ChatRepository,
    ExperienceRepository,
    JobRepository,
    LayoutRepository,
    MappingRepository,
    OutreachExampleRepository,
    OutreachRepository,
    ResumeRepository,
    RewriteRepository,
    SettingsRepository,
)


class Store:
    """Every repository over one database."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.resumes = ResumeRepository(database)
        self.jobs = JobRepository(database)
        self.mappings = MappingRepository(database)
        self.rewrites = RewriteRepository(database)
        self.outreach = OutreachRepository(database)
        self.examples = OutreachExampleRepository(database)
        self.experiences = ExperienceRepository(database)
        self.layouts = LayoutRepository(database)
        self.chat = ChatRepository(database)
        self.settings = SettingsRepository(database)

    def init(self) -> None:
        self.database.init()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Every repository call in the block commits together or not at all."""
        with self.database.transaction():
            yield


__all__ = [
    "Database",
    "Store",
    "ChatRepository",
    "ExperienceRepository",
    "JobRepository",
    "LayoutRepository",
    "MappingRepository",
    "OutreachExampleRepository",
    "OutreachRepository",
    "ResumeRepository",
    "RewriteRepository",
    "SettingsRepository",
]
