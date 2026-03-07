"""Tests for summary_logger.py — SummaryLogger."""

import re
import threading

import pytest

from database import Database
from summary_logger import MAX_ASSISTANT_TEXT, SummaryLogger


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def logger(db):
    return SummaryLogger(db)


class TestEntryFormat:
    def test_session_start_has_timestamp_and_heading(self, logger):
        logger.log_session_start("s1", "Fix bug #42")
        content = logger.get_content("s1")
        assert "Session Started" in content
        assert "**Prompt:** Fix bug #42" in content
        assert "---" in content

    def test_timestamp_format(self, logger):
        logger.log_session_start("s1")
        content = logger.get_content("s1")
        # Match [HH:MM:SS] compact timestamp format
        assert re.search(r"\[\d{2}:\d{2}:\d{2}\]", content)

    def test_assistant_text_entry(self, logger):
        logger.log_assistant_text("s1", "I'll fix the bug now.")
        content = logger.get_content("s1")
        assert "Assistant" in content
        assert "I'll fix the bug now." in content

    def test_error_entry(self, logger):
        logger.log_error("s1", "Connection refused")
        content = logger.get_content("s1")
        assert "Error" in content
        assert "Connection refused" in content

    def test_user_message_entry(self, logger):
        logger.log_user_message("s1", "Please also update tests")
        content = logger.get_content("s1")
        assert "User Message" in content
        assert "Please also update tests" in content

    def test_session_stop_entry(self, logger):
        logger.log_session_stop("s1", "Stopped by user")
        content = logger.get_content("s1")
        assert "Session Stopped" in content
        assert "**Reason:** Stopped by user" in content

    def test_session_resume_entry(self, logger):
        logger.log_session_resume("s1")
        content = logger.get_content("s1")
        assert "Session Resumed" in content


class TestAppendOnly:
    def test_multiple_entries_are_appended(self, logger):
        logger.log_session_start("s1", "prompt")
        logger.log_assistant_text("s1", "Working on it.")
        logger.log_assistant_text("s1", "Done.")
        content = logger.get_content("s1")
        assert "Session Started" in content
        assert "Working on it." in content
        assert "Done." in content
        # Verify order
        assert content.index("Session Started") < content.index("Working on it.")
        assert content.index("Working on it.") < content.index("Done.")

    def test_entries_survive_new_logger_instance(self, db):
        logger1 = SummaryLogger(db)
        logger1.log_session_start("s1", "prompt")

        logger2 = SummaryLogger(db)
        logger2.log_assistant_text("s1", "Continuing work.")
        content = logger2.get_content("s1")
        assert "Session Started" in content
        assert "Continuing work." in content


class TestGetContent:
    def test_returns_empty_for_unknown(self, logger):
        assert logger.get_content("nonexistent") == ""

    def test_returns_full_content(self, logger):
        logger.log_session_start("s1", "hello")
        content = logger.get_content("s1")
        assert len(content) > 0
        assert "hello" in content


class TestRemove:
    def test_remove_deletes_entries(self, logger):
        logger.log_session_start("s1", "prompt")
        assert logger.get_content("s1") != ""
        logger.remove("s1")
        assert logger.get_content("s1") == ""

    def test_remove_nonexistent_is_noop(self, logger):
        logger.remove("doesnt-exist")  # Should not raise


class TestTruncation:
    def test_assistant_text_truncated_at_limit(self, logger):
        long_text = "x" * (MAX_ASSISTANT_TEXT + 500)
        logger.log_assistant_text("s1", long_text)
        content = logger.get_content("s1")
        assert "... (truncated)" in content
        # The truncated text should be at most MAX_ASSISTANT_TEXT + overhead
        assert "x" * MAX_ASSISTANT_TEXT in content
        assert "x" * (MAX_ASSISTANT_TEXT + 1) not in content

    def test_short_text_not_truncated(self, logger):
        logger.log_assistant_text("s1", "short text")
        content = logger.get_content("s1")
        assert "truncated" not in content
        assert "short text" in content


class TestThreadSafety:
    def test_concurrent_writes(self, logger):
        errors = []

        def write_entries(session_name, count):
            try:
                for i in range(count):
                    logger.log_assistant_text(session_name, f"Message #{i}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=write_entries, args=(f"s{i}", 20))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # Each session should have entries
        for i in range(5):
            content = logger.get_content(f"s{i}")
            assert content != ""
            assert content.count("Assistant") == 20
