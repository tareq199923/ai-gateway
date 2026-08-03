# invincible/core/session_store.py
import json
import os
import time
import aiosqlite


def default_db_path() -> str:
    """Pick the session database location.

    Priority: explicit ``db_path`` argument (handled by the caller), then
    the INVINCIBLE_DB_PATH environment variable, then ``sessions.db`` in the
    current working directory. Never resolves inside the installed package.
    """
    env = os.getenv("INVINCIBLE_DB_PATH")
    if env:
        return env
    return os.path.join(os.getcwd(), "sessions.db")


class SessionStore:
    """Single-user local conversation memory backed by SQLite.

    Not a security boundary: session_id is a partition key, not a credential.
    Auth is handled entirely by GATEWAY_API_KEY upstream of this class.
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or default_db_path()
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                messages TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        await self._db.commit()

    async def close(self):
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def load(self, session_id: str) -> list:
        async with self._db.execute(
            "SELECT messages FROM sessions WHERE session_id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return []
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            # Corrupt row (manual edit, interrupted write): treat as empty
            # rather than crashing the request.
            return []

    async def save(self, session_id: str, messages: list):
        await self._db.execute(
            """
            INSERT INTO sessions (session_id, messages, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                messages = excluded.messages,
                updated_at = excluded.updated_at
            """,
            (session_id, json.dumps(messages), time.time()),
        )
        await self._db.commit()
