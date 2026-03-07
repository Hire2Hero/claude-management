"""Tests for history.py — SessionHistoryStore."""

import pytest

from database import Database
from history import MAX_ENTRIES, SessionHistoryStore


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def store(db):
    return SessionHistoryStore(db)


class TestAppend:
    def test_append_creates_entry(self, store):
        store.append("s1", "assistant", "hello")
        entries = store.get("s1")
        assert entries == [["assistant", "hello"]]

    def test_append_coalesces_same_tag(self, store):
        store.append("s1", "assistant", "hel")
        store.append("s1", "assistant", "lo")
        entries = store.get("s1")
        assert entries == [["assistant", "hello"]]

    def test_append_does_not_coalesce_different_tags(self, store):
        store.append("s1", "assistant", "hello")
        store.append("s1", "user", "world")
        entries = store.get("s1")
        assert entries == [["assistant", "hello"], ["user", "world"]]

    def test_append_coalesces_after_different_tag(self, store):
        store.append("s1", "assistant", "a")
        store.append("s1", "user", "b")
        store.append("s1", "user", "c")
        entries = store.get("s1")
        assert entries == [["assistant", "a"], ["user", "bc"]]

    def test_append_invalid_tag_becomes_system(self, store):
        store.append("s1", "bogus_tag", "text")
        entries = store.get("s1")
        assert entries == [["system", "text"]]

    def test_append_trims_to_max_entries(self, store):
        for i in range(MAX_ENTRIES + 100):
            # Use different tags to prevent coalescing
            tag = "assistant" if i % 2 == 0 else "user"
            store.append("s1", tag, f"entry-{i}")
        entries = store.get("s1")
        assert len(entries) == MAX_ENTRIES


class TestGet:
    def test_get_returns_empty_for_unknown(self, store):
        assert store.get("nonexistent") == []

    def test_get_returns_copy(self, store):
        store.append("s1", "assistant", "hello")
        entries1 = store.get("s1")
        entries2 = store.get("s1")
        entries1[0][1] = "modified"
        assert entries2[0][1] == "hello"

    def test_get_independent_sessions(self, store):
        store.append("s1", "assistant", "hello")
        store.append("s2", "user", "world")
        assert store.get("s1") == [["assistant", "hello"]]
        assert store.get("s2") == [["user", "world"]]


class TestFlush:
    def test_flush_persists_to_db(self, db, store):
        store.append("s1", "assistant", "hello")
        store.flush("s1")
        rows = db.fetchall(
            "SELECT tag, text FROM session_history WHERE session_name = ? ORDER BY id",
            ("s1",),
        )
        assert len(rows) == 1
        assert rows[0]["tag"] == "assistant"
        assert rows[0]["text"] == "hello"

    def test_flush_nonexistent_is_noop(self, store):
        store.flush("doesnt-exist")  # Should not raise

    def test_flush_all_persists_all(self, db, store):
        store.append("s1", "assistant", "one")
        store.append("s2", "user", "two")
        store.flush_all()
        rows = db.fetchall("SELECT DISTINCT session_name FROM session_history")
        assert len(rows) == 2

    def test_flush_and_reload_from_db(self, db):
        store1 = SessionHistoryStore(db)
        store1.append("s1", "assistant", "hello")
        store1.append("s1", "user", "world")
        store1.flush("s1")

        store2 = SessionHistoryStore(db)
        entries = store2.get("s1")
        assert entries == [["assistant", "hello"], ["user", "world"]]


class TestRemove:
    def test_remove_clears_memory(self, store):
        store.append("s1", "assistant", "hello")
        store.remove("s1")
        assert store.get("s1") == []

    def test_remove_deletes_from_db(self, db, store):
        store.append("s1", "assistant", "hello")
        store.flush("s1")
        store.remove("s1")
        rows = db.fetchall(
            "SELECT * FROM session_history WHERE session_name = ?", ("s1",)
        )
        assert len(rows) == 0

    def test_remove_nonexistent_is_noop(self, store):
        store.remove("doesnt-exist")  # Should not raise


class TestLazyLoad:
    def test_loads_from_db_on_first_access(self, db):
        # Pre-populate DB
        db.execute(
            "INSERT INTO session_history (session_name, tag, text) VALUES (?, ?, ?)",
            ("s1", "assistant", "from db"),
        )

        store = SessionHistoryStore(db)
        entries = store.get("s1")
        assert entries == [["assistant", "from db"]]

    def test_append_after_load_coalesces(self, db):
        db.execute(
            "INSERT INTO session_history (session_name, tag, text) VALUES (?, ?, ?)",
            ("s1", "assistant", "from db"),
        )

        store = SessionHistoryStore(db)
        store.append("s1", "assistant", " more")
        entries = store.get("s1")
        assert entries == [["assistant", "from db more"]]

    def test_only_loads_once(self, db):
        db.execute(
            "INSERT INTO session_history (session_name, tag, text) VALUES (?, ?, ?)",
            ("s1", "assistant", "original"),
        )

        store = SessionHistoryStore(db)
        entries1 = store.get("s1")
        assert entries1 == [["assistant", "original"]]

        # Modify DB directly — should NOT be re-loaded
        db.execute("DELETE FROM session_history WHERE session_name = ?", ("s1",))
        db.execute(
            "INSERT INTO session_history (session_name, tag, text) VALUES (?, ?, ?)",
            ("s1", "assistant", "changed"),
        )

        entries2 = store.get("s1")
        assert entries2 == [["assistant", "original"]]


class TestSpecialChars:
    def test_special_chars_in_name(self, store):
        store.append("Fix Repo#42: stuff", "assistant", "hello")
        store.flush("Fix Repo#42: stuff")
        entries = store.get("Fix Repo#42: stuff")
        assert entries == [["assistant", "hello"]]
