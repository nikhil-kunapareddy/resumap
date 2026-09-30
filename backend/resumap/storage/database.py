"""The SQLite file, and how a unit of work gets a connection.

Every repository call runs through ``Database.connect()``. Outside a
transaction that is a fresh connection, committed (or rolled back) and closed
when the call returns. Inside ``Database.transaction()`` every call joins one
shared connection instead, so a multi-step write lands whole or not at all.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .schema import migrate

# The connection of the transaction open in this context, and whose it is.
# Contexts are per thread and per asyncio task, so concurrent requests never
# see each other's transaction.
_open_transaction: ContextVar[tuple[Database, sqlite3.Connection] | None] = ContextVar(
    "resumap_open_transaction", default=None
)


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def init(self) -> None:
        """Create the file and any missing tables or columns. Safe to repeat."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            migrate(conn)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """The open transaction's connection, or one of its own for a single call."""
        current = _open_transaction.get()
        if current is not None and current[0] is self:
            yield current[1]
            return
        conn = self._open()
        try:
            with conn:  # commit on success, roll back on an exception
                yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run every repository call in the block as one transaction.

        A transaction opened inside another joins it, so a helper can ask for
        atomicity without knowing whether its caller already has.
        """
        current = _open_transaction.get()
        if current is not None and current[0] is self:
            yield
            return
        with self.connect() as conn:
            token = _open_transaction.set((self, conn))
            try:
                yield
            finally:
                _open_transaction.reset(token)
