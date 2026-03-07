"""Tests for database.py — schema creation, WAL, thread safety, auto-migration."""

import json
import os
import threading

import pytest

from database import Database, SCHEMA_VERSION


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


class TestSchemaCreation:
    def test_all_tables_exist(self, db):
        rows = db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {r["name"] for r in rows}
        assert "sessions" in tables
        assert "tracked_prs" in tables
        assert "session_history" in tables
        assert "session_summaries" in tables
        assert "schema_version" in tables

    def test_schema_version_set(self, db):
        row = db.fetchone("SELECT version FROM schema_version")
        assert row["version"] == SCHEMA_VERSION

    def test_indexes_created(self, db):
        rows = db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
        names = {r["name"] for r in rows}
        assert "idx_history_session" in names
        assert "idx_summary_session" in names

    def test_idempotent_schema(self, db):
        """Creating a second Database on the same connection shouldn't fail."""
        db._ensure_schema()
        row = db.fetchone("SELECT version FROM schema_version")
        assert row["version"] == SCHEMA_VERSION


class TestWALMode:
    def test_wal_enabled(self, db):
        row = db.fetchone("PRAGMA journal_mode")
        # In-memory databases may report 'memory' instead of 'wal'
        # but file-based databases will report 'wal'
        assert row[0] in ("wal", "memory")


class TestCRUD:
    def test_execute_and_fetchall(self, db):
        db.execute(
            "INSERT INTO sessions (name, repo, status) VALUES (?, ?, ?)",
            ("s1", "RepoA", "running"),
        )
        rows = db.fetchall("SELECT * FROM sessions")
        assert len(rows) == 1
        assert rows[0]["name"] == "s1"

    def test_fetchone_returns_none(self, db):
        row = db.fetchone("SELECT * FROM sessions WHERE name = ?", ("nope",))
        assert row is None

    def test_executemany(self, db):
        db.executemany(
            "INSERT INTO session_history (session_name, tag, text) VALUES (?, ?, ?)",
            [("s1", "user", "hello"), ("s1", "assistant", "hi")],
        )
        rows = db.fetchall("SELECT * FROM session_history WHERE session_name = ?", ("s1",))
        assert len(rows) == 2


class TestThreadSafety:
    def test_concurrent_writes(self, db):
        errors = []

        def writer(session_name: str, count: int):
            try:
                for i in range(count):
                    db.execute(
                        "INSERT INTO session_history (session_name, tag, text) VALUES (?, ?, ?)",
                        (session_name, "user", f"msg-{i}"),
                    )
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(f"s{i}", 50))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        rows = db.fetchall("SELECT COUNT(*) as c FROM session_history")
        assert rows[0]["c"] == 250


class TestFileBased:
    def test_wal_on_file_database(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        d = Database(db_path)
        row = d.fetchone("PRAGMA journal_mode")
        assert row[0] == "wal"
        d.close()

    def test_data_persists_across_instances(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        d1 = Database(db_path)
        d1.execute(
            "INSERT INTO sessions (name, repo, status) VALUES (?, ?, ?)",
            ("s1", "RepoA", "running"),
        )
        d1.close()

        d2 = Database(db_path)
        rows = d2.fetchall("SELECT * FROM sessions")
        assert len(rows) == 1
        assert rows[0]["name"] == "s1"
        d2.close()


class TestAutoMigration:
    """Test the auto-migration logic from main.py using file fixtures."""

    def test_migrates_state_json(self, tmp_path, monkeypatch):
        """Verify state.json sessions and tracked_prs are migrated."""
        # Set up mock APP_DIR
        app_dir = str(tmp_path)
        state_path = os.path.join(app_dir, "state.json")
        db_path = os.path.join(app_dir, "data.db")

        state_data = {
            "sessions": {
                "my-session": {
                    "name": "my-session",
                    "repo": "RepoA",
                    "pid": 123,
                    "session_id": "sid-1",
                    "status": "running",
                    "created_at": 1000.0,
                    "ticket_id": "KAN-5",
                    "cwd": "/some/path",
                }
            },
            "tracked_prs": {
                "RepoA#1": {
                    "repo": "RepoA",
                    "number": 1,
                    "branch": "feat/test",
                    "last_issue": "ci_failing",
                    "last_action": "launch_claude",
                    "last_action_time": 500.0,
                    "claude_pid": None,
                    "ci_was_failing": True,
                    "slack_sent": False,
                }
            },
        }
        with open(state_path, "w") as f:
            json.dump(state_data, f)

        # Run migration using the Application._auto_migrate pattern
        db = Database(db_path)

        # Import and run the migration logic directly
        import main as main_mod
        monkeypatch.setattr(main_mod, "APP_DIR", app_dir)

        # Create a minimal object to call _auto_migrate
        class FakeApp:
            _db = db
        FakeApp._auto_migrate = main_mod.Application._auto_migrate
        FakeApp._auto_migrate(FakeApp)

        # Verify sessions migrated
        rows = db.fetchall("SELECT * FROM sessions")
        assert len(rows) == 1
        assert rows[0]["name"] == "my-session"
        assert rows[0]["repo"] == "RepoA"
        assert rows[0]["ticket_id"] == "KAN-5"

        # Verify tracked_prs migrated
        rows = db.fetchall("SELECT * FROM tracked_prs")
        assert len(rows) == 1
        assert rows[0]["key"] == "RepoA#1"
        assert rows[0]["branch"] == "feat/test"
        assert rows[0]["ci_was_failing"] == 1

        # Verify old file renamed
        assert os.path.exists(state_path + ".migrated")
        assert not os.path.exists(state_path)

        db.close()

    def test_migrates_history_files(self, tmp_path, monkeypatch):
        """Verify session_history/*.json files are migrated."""
        app_dir = str(tmp_path)
        db_path = os.path.join(app_dir, "data.db")
        history_dir = os.path.join(app_dir, "session_history")
        os.makedirs(history_dir)

        # Also need state.json to map slugs to names
        state_data = {
            "sessions": {
                "Fix Repo#42": {
                    "name": "Fix Repo#42",
                    "repo": "Repo",
                    "status": "stopped",
                }
            }
        }
        with open(os.path.join(app_dir, "state.json"), "w") as f:
            json.dump(state_data, f)

        # Create history file with slugified name
        with open(os.path.join(history_dir, "Fix_Repo_42.json"), "w") as f:
            json.dump([["assistant", "hello"], ["user", "world"]], f)

        db = Database(db_path)

        import main as main_mod
        monkeypatch.setattr(main_mod, "APP_DIR", app_dir)

        class FakeApp:
            _db = db
        FakeApp._auto_migrate = main_mod.Application._auto_migrate
        FakeApp._auto_migrate(FakeApp)

        rows = db.fetchall(
            "SELECT * FROM session_history WHERE session_name = ? ORDER BY id",
            ("Fix Repo#42",),
        )
        assert len(rows) == 2
        assert rows[0]["tag"] == "assistant"
        assert rows[0]["text"] == "hello"
        assert rows[1]["tag"] == "user"

        # Verify old dir renamed
        assert os.path.exists(history_dir + ".migrated")
        assert not os.path.exists(history_dir)

        db.close()

    def test_migrates_summary_files(self, tmp_path, monkeypatch):
        """Verify session_summaries/*.md files are migrated."""
        app_dir = str(tmp_path)
        db_path = os.path.join(app_dir, "data.db")
        summaries_dir = os.path.join(app_dir, "session_summaries")
        os.makedirs(summaries_dir)

        md_content = (
            "## 2024-06-01 10:00:00 — Session Started\n"
            "**Prompt:** Fix the bug\n"
            "\n---\n\n"
            "## 2024-06-01 10:01:00 — Assistant\n"
            "I'll fix it now.\n"
            "\n---\n\n"
        )
        with open(os.path.join(summaries_dir, "s1.md"), "w") as f:
            f.write(md_content)

        db = Database(db_path)

        import main as main_mod
        monkeypatch.setattr(main_mod, "APP_DIR", app_dir)

        class FakeApp:
            _db = db
        FakeApp._auto_migrate = main_mod.Application._auto_migrate
        FakeApp._auto_migrate(FakeApp)

        rows = db.fetchall(
            "SELECT * FROM session_summaries WHERE session_name = ? ORDER BY id",
            ("s1",),
        )
        assert len(rows) == 2
        assert rows[0]["entry_type"] == "Session Started"
        assert "Fix the bug" in rows[0]["content"]
        assert rows[0]["created_at"] == "2024-06-01 10:00:00"
        assert rows[1]["entry_type"] == "Assistant"

        # Verify old dir renamed
        assert os.path.exists(summaries_dir + ".migrated")

        db.close()

    def test_skips_migration_if_db_has_data(self, tmp_path, monkeypatch):
        """Migration should not run if DB already has sessions."""
        app_dir = str(tmp_path)
        db_path = os.path.join(app_dir, "data.db")
        state_path = os.path.join(app_dir, "state.json")

        with open(state_path, "w") as f:
            json.dump({"sessions": {"old": {"name": "old", "repo": "R", "status": "stopped"}}}, f)

        db = Database(db_path)
        # Pre-populate DB
        db.execute(
            "INSERT INTO sessions (name, repo, status) VALUES (?, ?, ?)",
            ("existing", "R", "stopped"),
        )

        import main as main_mod
        monkeypatch.setattr(main_mod, "APP_DIR", app_dir)

        class FakeApp:
            _db = db
        FakeApp._auto_migrate = main_mod.Application._auto_migrate
        FakeApp._auto_migrate(FakeApp)

        # state.json should NOT have been renamed (migration skipped)
        assert os.path.exists(state_path)

        # DB should only have the pre-existing session
        rows = db.fetchall("SELECT * FROM sessions")
        assert len(rows) == 1
        assert rows[0]["name"] == "existing"

        db.close()
