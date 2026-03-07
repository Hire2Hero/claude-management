"""Per-session chat history — in-memory cache + SQLite persistence."""

from __future__ import annotations

import logging
import threading

from database import Database

log = logging.getLogger("claude_mgmt")

MAX_ENTRIES = 10_000

# Valid tags matching ChatPanel's tag system
VALID_TAGS = {"assistant", "user", "tool", "error", "system"}


class SessionHistoryStore:
    """Manages per-session chat history with SQLite persistence.

    Each session's history is a list of ``(tag, text)`` pairs that mirror
    what ChatPanel displays.  Consecutive entries with the same tag are
    coalesced so that streaming deltas don't create thousands of entries.

    An in-memory cache is kept for streaming performance — identical to the
    previous file-based behavior.
    """

    def __init__(self, db: Database):
        self._db = db
        self._lock = threading.Lock()
        # name -> list of [tag, text]
        self._store: dict[str, list[list[str]]] = {}
        # Track which sessions have been loaded from DB
        self._loaded: set[str] = set()

    # ── Public API ────────────────────────────────────────────────────────

    def append(self, name: str, tag: str, text: str):
        """Append an entry, coalescing with the previous entry if same tag."""
        if tag not in VALID_TAGS:
            tag = "system"
        with self._lock:
            entries = self._get_or_load(name)
            if entries and entries[-1][0] == tag:
                entries[-1][1] += text
            else:
                entries.append([tag, text])
            # Trim to max
            if len(entries) > MAX_ENTRIES:
                entries[:] = entries[-MAX_ENTRIES:]

    def get(self, name: str) -> list[list[str]]:
        """Return a copy of the history for *name*."""
        with self._lock:
            entries = self._get_or_load(name)
            return [list(e) for e in entries]

    def flush(self, name: str):
        """Persist a single session's history to the database."""
        with self._lock:
            entries = self._store.get(name)
            if entries is None:
                return
            self._write(name, entries)

    def flush_all(self):
        """Persist all in-memory histories to the database."""
        with self._lock:
            for name, entries in self._store.items():
                self._write(name, entries)

    def remove(self, name: str):
        """Delete history for *name* from memory and database."""
        with self._lock:
            self._store.pop(name, None)
            self._loaded.discard(name)
            self._db.execute(
                "DELETE FROM session_history WHERE session_name = ?", (name,)
            )

    # ── Internal ──────────────────────────────────────────────────────────

    def _get_or_load(self, name: str) -> list[list[str]]:
        """Return the entry list for *name*, lazy-loading from DB if needed.

        Caller must hold ``self._lock``.
        """
        if name not in self._store:
            if name not in self._loaded:
                self._load(name)
            if name not in self._store:
                self._store[name] = []
        return self._store[name]

    def _load(self, name: str):
        """Load history from DB into ``self._store``.  Caller must hold lock."""
        self._loaded.add(name)
        rows = self._db.fetchall(
            "SELECT tag, text FROM session_history "
            "WHERE session_name = ? ORDER BY id",
            (name,),
        )
        if rows:
            self._store[name] = [[r["tag"], r["text"]] for r in rows]
            log.info("Loaded %d history entries for '%s'", len(rows), name)

    def _write(self, name: str, entries: list[list[str]]):
        """Write entries to DB.  Caller must hold lock."""
        self._db.execute(
            "DELETE FROM session_history WHERE session_name = ?", (name,)
        )
        if entries:
            self._db.executemany(
                "INSERT INTO session_history (session_name, tag, text) "
                "VALUES (?, ?, ?)",
                [(name, tag, text) for tag, text in entries],
            )
