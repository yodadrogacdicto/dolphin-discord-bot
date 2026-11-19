"""Opt-in, per-user memory stored in SQLite."""
import asyncio
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.getenv(
    "MEMORY_DB_PATH",
    str(Path(__file__).resolve().parent / "data" / "memories.sqlite3"),
)
MAX_FACTS_PER_USER = 40
MAX_FACT_LENGTH = 500


def _connect():
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10)
    db.execute("PRAGMA busy_timeout = 10000")
    db.execute(
        """CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            fact TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, fact)
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS memories_user_id ON memories(user_id, id)")
    db.commit()
    return db


@contextmanager
def _database():
    db = _connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _init_sync():
    with _database() as db:
        db.execute("PRAGMA journal_mode = WAL")


def _add_sync(user_id, fact):
    with _database() as db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO memories (user_id, fact) VALUES (?, ?)",
            (str(user_id), fact),
        )
        added = cursor.rowcount > 0
        if added:
            db.execute(
                """DELETE FROM memories WHERE user_id = ? AND id NOT IN (
                    SELECT id FROM memories WHERE user_id = ?
                    ORDER BY id DESC LIMIT ?
                )""",
                (str(user_id), str(user_id), MAX_FACTS_PER_USER),
            )
            row = db.execute(
                "SELECT id FROM memories WHERE user_id = ? AND fact = ?",
                (str(user_id), fact),
            ).fetchone()
            return row[0], True
        row = db.execute(
            "SELECT id FROM memories WHERE user_id = ? AND fact = ?",
            (str(user_id), fact),
        ).fetchone()
        return (row[0] if row else None), False


def _list_sync(user_id):
    with _database() as db:
        return db.execute(
            "SELECT id, fact FROM memories WHERE user_id = ? ORDER BY id",
            (str(user_id),),
        ).fetchall()


def _delete_sync(user_id, memory_id=None):
    with _database() as db:
        if memory_id is None:
            cursor = db.execute("DELETE FROM memories WHERE user_id = ?", (str(user_id),))
        else:
            cursor = db.execute(
                "DELETE FROM memories WHERE user_id = ? AND id = ?",
                (str(user_id), int(memory_id)),
            )
        return cursor.rowcount


async def init_store():
    await asyncio.to_thread(_init_sync)


async def add_fact(user_id, fact):
    fact = " ".join(str(fact).split()).strip()
    if not fact:
        raise ValueError("La memoria no puede estar vacía.")
    if len(fact) > MAX_FACT_LENGTH:
        raise ValueError(f"Cada recuerdo puede tener hasta {MAX_FACT_LENGTH} caracteres.")
    return await asyncio.to_thread(_add_sync, user_id, fact)


async def list_facts(user_id):
    return await asyncio.to_thread(_list_sync, user_id)


async def delete_fact(user_id, memory_id=None):
    return await asyncio.to_thread(_delete_sync, user_id, memory_id)
