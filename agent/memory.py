from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class Memory:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    user_input TEXT NOT NULL,
                    assistant_output TEXT NOT NULL,
                    skills TEXT NOT NULL DEFAULT '',
                    rating INTEGER,
                    corrected TEXT,
                    note TEXT,
                    rated_at TEXT
                )
                """
            )

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record_turn(self, user_input: str, assistant_output: str, skills: list[str]) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO turns (created_at, user_input, assistant_output, skills) VALUES (?, ?, ?, ?)",
                (datetime.now().isoformat(timespec="seconds"), user_input, assistant_output, ",".join(skills)),
            )
            return int(cursor.lastrowid)

    def last_turn(self) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM turns ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def rate(self, turn_id: int, rating: int, corrected: str | None = None, note: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE turns SET rating = ?, corrected = ?, note = ?, rated_at = ? WHERE id = ?",
                (rating, corrected, note, datetime.now().isoformat(timespec="seconds"), turn_id),
            )

    def stats(self) -> dict:
        with self._connect() as conn:
            total, rated, avg = conn.execute(
                "SELECT COUNT(*), COUNT(rating), AVG(rating) FROM turns"
            ).fetchone()
            by_skill = conn.execute(
                "SELECT skills, COUNT(*) FROM turns WHERE skills != '' "
                "GROUP BY skills ORDER BY COUNT(*) DESC LIMIT 5"
            ).fetchall()
        return {
            "total": total,
            "rated": rated,
            "avg": round(avg, 1) if avg is not None else None,
            "by_skill": [(row[0], row[1]) for row in by_skill],
        }

    def turns_with_min_rating(self, min_rating: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM turns WHERE rating IS NOT NULL AND rating >= ? ORDER BY id",
                (min_rating,),
            ).fetchall()
        return [dict(row) for row in rows]

    def turns_for_dpo(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM turns "
                "WHERE rating IS NOT NULL AND rating <= 2 "
                "AND corrected IS NOT NULL AND TRIM(corrected) != '' "
                "ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]
