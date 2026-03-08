"""Session discovery, registry, and state persistence."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

from config import Config
from database import Database
from models import ManagedSession, SessionStatus, is_pid_alive, extract_ticket_id

log = logging.getLogger("claude_mgmt")


class SessionManager:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self._db = db
        self._lock = threading.Lock()

    def find_claude_session(self, repo: str, ticket_id: Optional[str] = None) -> Optional[str]:
        """Search sessions-index.json for a matching session by branch/ticket."""
        if not ticket_id:
            return None
        project_slug = f"-Users-nicholasl-Documents-Programming-{repo}"
        index_path = os.path.join(self.config.claude_projects_dir, project_slug, "sessions-index.json")
        if not os.path.exists(index_path):
            return None
        try:
            with open(index_path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

        matches = []
        for entry in data.get("entries", []):
            branch = entry.get("gitBranch", "")
            if ticket_id in branch:
                matches.append(entry)

        if not matches:
            return None

        matches.sort(key=lambda e: e.get("modified", ""), reverse=True)
        session_id = matches[0].get("sessionId")
        log.info("Found session %s for %s on branch %s", session_id, ticket_id, matches[0].get("gitBranch"))
        return session_id

    def register_session(self, session: ManagedSession):
        with self._lock:
            if not session.created_at:
                session.created_at = time.time()
            if not session.ticket_id:
                session.ticket_id = extract_ticket_id(session.name)
            self._db.execute(
                "INSERT OR REPLACE INTO sessions "
                "(name, repo, pid, session_id, status, created_at, ticket_id, cwd, pr_url, needs_input, last_response_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session.name,
                    session.repo,
                    session.pid,
                    session.session_id,
                    session.status.value,
                    session.created_at,
                    session.ticket_id,
                    session.cwd,
                    session.pr_url,
                    int(session.needs_input),
                    session.last_response_at,
                ),
            )
        log.info("Registered session: %s (repo=%s, pid=%s)", session.name, session.repo, session.pid)

    def set_needs_input(self, name: str, value: bool):
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET needs_input = ? WHERE name = ?",
                (int(value), name),
            )

    def set_last_response_at(self, name: str, timestamp: float):
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET last_response_at = ? WHERE name = ?",
                (timestamp, name),
            )

    def unregister_session(self, name: str):
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE name = ?", (name,))

    def refresh_statuses(self, running_names: set[str] | None = None):
        """Reconcile session statuses.

        For sessions with a PID, check if the process is alive (legacy).
        For sessions without a PID (SDK-managed), use running_names from
        the in-memory ClaudeProcess map to determine liveness.
        """
        with self._lock:
            rows = self._db.fetchall("SELECT name, pid, status FROM sessions")
            running = running_names or set()
            for row in rows:
                if row["pid"]:
                    alive = is_pid_alive(row["pid"])
                else:
                    alive = row["name"] in running
                new_status = SessionStatus.RUNNING if alive else SessionStatus.STOPPED
                if row["status"] != new_status.value:
                    self._db.execute(
                        "UPDATE sessions SET status = ? WHERE name = ?",
                        (new_status.value, row["name"]),
                    )

    def get_all_sessions(self) -> list[ManagedSession]:
        with self._lock:
            rows = self._db.fetchall("SELECT * FROM sessions")
            return [
                ManagedSession(
                    name=r["name"],
                    repo=r["repo"],
                    pid=r["pid"],
                    session_id=r["session_id"],
                    status=SessionStatus(r["status"]),
                    created_at=r["created_at"],
                    ticket_id=r["ticket_id"],
                    cwd=r["cwd"],
                    pr_url=r["pr_url"],
                    needs_input=bool(r["needs_input"]),
                    last_response_at=r["last_response_at"],
                )
                for r in rows
            ]
