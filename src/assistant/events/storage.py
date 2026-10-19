"""SQLite stand-in for the PostgreSQL storage owned by the backend team.

Keeps the same contract as the MCP tools so the agent can be developed and
evaluated before the real database exists. Datetimes are stored in UTC and
returned in the configured timezone.
"""

import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from assistant.events.models import Event, EventStatus, EventType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    ts_utc      TEXT,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS ix_events_user_ts ON events (user_id, ts_utc);
"""

_WORD_RE = re.compile(r"[a-zа-яё0-9]+")

# Words that carry no meaning for search ("найди задачу про ...").
_STOP_WORDS = frozenset(
    [
        "и",
        "в",
        "во",
        "на",
        "с",
        "со",
        "по",
        "про",
        "о",
        "об",
        "к",
        "ко",
        "у",
        "за",
        "для",
        "из",
        "от",
        "до",
        "я",
        "мне",
        "меня",
        "мой",
        "моя",
        "мои",
        "это",
        "эта",
        "этот",
        "тот",
        "та",
        "когда-то",
        "когда",
        "где",
        "что",
        "как",
        "найди",
        "найти",
        "покажи",
        "задача",
        "задачу",
        "задачи",
        "встреча",
        "встречу",
        "встречи",
        "напоминание",
        "напоминания",
        "была",
        "был",
        "было",
        "ставил",
        "ставила",
        "её",
        "ее",
        "его",
    ]
)


def _stem(word: str) -> str:
    """Crude stemming: Russian inflection mostly changes the last 1-2 letters."""
    return word if len(word) <= 4 else word[: max(4, len(word) - 2)]


def _search_terms(text: str) -> list[str]:
    words = _WORD_RE.findall(text.lower().replace("ё", "е"))
    return [_stem(w) for w in words if w not in _STOP_WORDS and len(w) > 1]


def _to_utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds")


class EventStorage:
    def __init__(self, path: Path | str, tz: ZoneInfo) -> None:
        self._tz = tz
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def create(
        self,
        user_id: str,
        event_type: EventType,
        name: str,
        description: str,
        when: datetime | None,
    ) -> Event:
        created_at = datetime.now(UTC)
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO events (user_id, event_type, name, description, ts_utc, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    event_type.value,
                    name,
                    description,
                    _to_utc_text(when) if when else None,
                    created_at.isoformat(timespec="seconds"),
                ),
            )
        row = self._conn.execute("SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._to_event(row)

    def list_between(
        self,
        user_id: str,
        event_types: Sequence[EventType],
        start: datetime,
        end: datetime,
    ) -> list[Event]:
        types = list(event_types) or list(EventType)
        placeholders = ",".join("?" * len(types))
        rows = self._conn.execute(
            f"SELECT * FROM events WHERE user_id = ? AND event_type IN ({placeholders})"
            " AND ts_utc >= ? AND ts_utc <= ? ORDER BY ts_utc",
            (user_id, *[t.value for t in types], _to_utc_text(start), _to_utc_text(end)),
        ).fetchall()
        return [self._to_event(r) for r in rows]

    def search(
        self,
        user_id: str,
        text: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 10,
    ) -> list[Event]:
        """Rank user's events by how many query terms occur in name/description."""
        terms = _search_terms(text)
        if not terms:
            return []
        query = "SELECT * FROM events WHERE user_id = ?"
        params: list[str] = [user_id]
        if start:
            query += " AND ts_utc >= ?"
            params.append(_to_utc_text(start))
        if end:
            query += " AND ts_utc <= ?"
            params.append(_to_utc_text(end))
        scored: list[tuple[int, Event]] = []
        for row in self._conn.execute(query, params):
            haystack = f"{row['name']} {row['description']}".lower().replace("ё", "е")
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, self._to_event(row)))
        scored.sort(key=lambda item: -item[0])
        return [event for _, event in scored[:limit]]

    def _to_event(self, row: sqlite3.Row) -> Event:
        ts = row["ts_utc"]
        return Event(
            id=row["id"],
            user_id=row["user_id"],
            event_type=EventType(row["event_type"]),
            name=row["name"],
            description=row["description"],
            when=datetime.fromisoformat(ts).astimezone(self._tz) if ts else None,
            created_at=datetime.fromisoformat(row["created_at"]).astimezone(self._tz),
            status=EventStatus(row["status"]),
        )
