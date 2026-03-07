"""Tests for session_manager.py — registry, discovery, persistence."""

import json
import os

import pytest

from config import Config
from database import Database
from models import ManagedSession, SessionStatus
from session_manager import SessionManager


@pytest.fixture
def config(tmp_path):
    return Config(
        org="TestOrg",
        repos=["RepoA"],
        base_dir=str(tmp_path),
        claude_projects_dir=str(tmp_path / "projects"),
    )


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def mgr(config, db):
    return SessionManager(config, db)


class TestRegisterSession:
    def test_register_adds_session(self, mgr):
        s = ManagedSession(name="test-session", repo="RepoA", pid=123)
        mgr.register_session(s)

        sessions = mgr.get_all_sessions()
        assert len(sessions) == 1
        assert sessions[0].name == "test-session"
        assert sessions[0].repo == "RepoA"
        assert sessions[0].pid == 123

    def test_register_sets_created_at(self, mgr):
        s = ManagedSession(name="ts-test", repo="RepoA")
        mgr.register_session(s)
        sessions = mgr.get_all_sessions()
        assert sessions[0].created_at > 0

    def test_register_extracts_ticket_from_name(self, mgr):
        s = ManagedSession(name="KAN-42-feature", repo="RepoA")
        mgr.register_session(s)
        sessions = mgr.get_all_sessions()
        assert sessions[0].ticket_id == "KAN-42"

    def test_register_preserves_explicit_ticket(self, mgr):
        s = ManagedSession(name="custom-name", repo="RepoA", ticket_id="PROJ-99")
        mgr.register_session(s)
        sessions = mgr.get_all_sessions()
        assert sessions[0].ticket_id == "PROJ-99"

    def test_data_survives_new_manager_instance(self, config, db):
        mgr1 = SessionManager(config, db)
        mgr1.register_session(ManagedSession(name="persist-test", repo="RepoA", pid=42))

        mgr2 = SessionManager(config, db)
        sessions = mgr2.get_all_sessions()
        assert len(sessions) == 1
        assert sessions[0].name == "persist-test"
        assert sessions[0].pid == 42


class TestUnregisterSession:
    def test_unregister_removes_session(self, mgr):
        mgr.register_session(ManagedSession(name="to-remove", repo="RepoA"))
        mgr.unregister_session("to-remove")
        assert mgr.get_all_sessions() == []

    def test_unregister_nonexistent_is_noop(self, mgr):
        mgr.unregister_session("doesnt-exist")  # Should not raise


class TestRefreshStatuses:
    def test_marks_dead_pid_as_stopped(self, mgr):
        s = ManagedSession(name="dead", repo="RepoA", pid=99999999, status=SessionStatus.RUNNING)
        mgr.register_session(s)
        mgr.refresh_statuses()

        sessions = mgr.get_all_sessions()
        assert sessions[0].status == SessionStatus.STOPPED

    def test_marks_alive_pid_as_running(self, mgr):
        s = ManagedSession(name="alive", repo="RepoA", pid=os.getpid(), status=SessionStatus.RUNNING)
        mgr.register_session(s)
        mgr.refresh_statuses()

        sessions = mgr.get_all_sessions()
        assert sessions[0].status == SessionStatus.RUNNING

    def test_marks_none_pid_as_stopped(self, mgr):
        s = ManagedSession(name="no-pid", repo="RepoA", pid=None, status=SessionStatus.RUNNING)
        mgr.register_session(s)
        mgr.refresh_statuses()

        sessions = mgr.get_all_sessions()
        assert sessions[0].status == SessionStatus.STOPPED


class TestFindClaudeSession:
    def test_finds_session_by_ticket(self, config, db):
        mgr = SessionManager(config, db)

        project_dir = os.path.join(config.claude_projects_dir, "-Users-nicholasl-Documents-Programming-RepoA")
        os.makedirs(project_dir, exist_ok=True)
        index_data = {
            "entries": [
                {"sessionId": "session-old", "gitBranch": "feat/KAN-10-old", "modified": "2024-01-01"},
                {"sessionId": "session-new", "gitBranch": "feat/KAN-10-new", "modified": "2024-06-01"},
                {"sessionId": "session-other", "gitBranch": "feat/KAN-20-other", "modified": "2024-03-01"},
            ]
        }
        with open(os.path.join(project_dir, "sessions-index.json"), "w") as f:
            json.dump(index_data, f)

        result = mgr.find_claude_session("RepoA", "KAN-10")
        assert result == "session-new"

    def test_returns_none_for_no_match(self, config, db):
        mgr = SessionManager(config, db)
        project_dir = os.path.join(config.claude_projects_dir, "-Users-nicholasl-Documents-Programming-RepoA")
        os.makedirs(project_dir, exist_ok=True)
        with open(os.path.join(project_dir, "sessions-index.json"), "w") as f:
            json.dump({"entries": [{"sessionId": "s1", "gitBranch": "main", "modified": "2024-01-01"}]}, f)

        result = mgr.find_claude_session("RepoA", "KAN-999")
        assert result is None

    def test_returns_none_for_no_ticket(self, mgr):
        assert mgr.find_claude_session("RepoA", None) is None

    def test_returns_none_for_missing_index(self, mgr):
        assert mgr.find_claude_session("NonexistentRepo", "KAN-1") is None

    def test_returns_none_for_corrupt_index(self, config, db):
        mgr = SessionManager(config, db)
        project_dir = os.path.join(config.claude_projects_dir, "-Users-nicholasl-Documents-Programming-RepoA")
        os.makedirs(project_dir, exist_ok=True)
        with open(os.path.join(project_dir, "sessions-index.json"), "w") as f:
            f.write("bad json")

        assert mgr.find_claude_session("RepoA", "KAN-1") is None
