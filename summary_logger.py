"""Per-session summary logger — append-only timestamped entries in SQLite."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from database import Database

log = logging.getLogger("claude_mgmt")

MAX_ASSISTANT_TEXT = 2_000


class SummaryLogger:
    """Writes timestamped summary entries to the ``session_summaries`` table.

    Each write inserts one row.  ``get_content()`` renders all rows back to
    the same markdown format the old file-based logger produced.
    """

    def __init__(self, db: Database):
        self._db = db
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    def log_session_start(self, name: str, prompt: str | None = None):
        lines = []
        if prompt:
            lines.append(f"**Prompt:** {prompt}")
        self._insert(name, "Session Started", "\n".join(lines))

    def log_assistant_text(self, name: str, text: str):
        truncated = text[:MAX_ASSISTANT_TEXT]
        if len(text) > MAX_ASSISTANT_TEXT:
            truncated += "... (truncated)"
        self._insert(name, "Assistant", truncated)

    def log_error(self, name: str, text: str):
        self._insert(name, "Error", text)

    def log_user_message(self, name: str, text: str):
        self._insert(name, "User Message", text)

    def log_session_stop(self, name: str, reason: str = ""):
        content = f"**Reason:** {reason}" if reason else ""
        self._insert(name, "Session Stopped", content)

    def log_session_resume(self, name: str):
        self._insert(name, "Session Resumed", "")

    def get_content(self, name: str) -> str:
        """Return the rendered markdown content for *name*, or empty string."""
        with self._lock:
            rows = self._db.fetchall(
                "SELECT entry_type, content, created_at "
                "FROM session_summaries WHERE session_name = ? ORDER BY id",
                (name,),
            )
            if not rows:
                return ""
            parts = []
            for r in rows:
                parts.append(self._render_entry(r["entry_type"], r["content"], r["created_at"]))
            return "".join(parts)

    def remove(self, name: str):
        """Delete all summary entries for *name*."""
        with self._lock:
            self._db.execute(
                "DELETE FROM session_summaries WHERE session_name = ?", (name,)
            )

    # ── Internal ──────────────────────────────────────────────────────────

    def _insert(self, name: str, entry_type: str, content: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._db.execute(
                "INSERT INTO session_summaries "
                "(session_name, entry_type, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (name, entry_type, content, ts),
            )

    @staticmethod
    def _render_entry(entry_type: str, content: str, created_at: str) -> str:
        # Extract just the time portion (HH:MM:SS) for compactness
        time_part = created_at.split(" ", 1)[1] if " " in created_at else created_at
        heading = f"[{time_part}] {entry_type}\n"
        body = f"{content}\n" if content else ""
        return heading + body + "---\n"
