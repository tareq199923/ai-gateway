# gateway/core/session_store.py
import json
import os
import time
import aiosqlite

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "sessions.db"
)


class SessionStore:
    """Single-user local conversation memory backed by SQLite.

    Not a security boundary: session_id is a partition key, not a credential.
    Auth is handled entirely by GATEWAY_API_KEY upstream of this class.
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DEFAULT_DB_PATH
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
        return json.loads(row[0])

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
