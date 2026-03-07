"""Tests for pr_monitor.py — StateTracker, PRMonitorThread, Prompts."""

import os
import queue
import time
from unittest.mock import MagicMock, patch

import pytest

from config import Config
from database import Database
from models import (
    CheckRun, PRAction, PRData, PRIssueType, TrackedPR,
)
from pr_monitor import PRMonitorThread, Prompts, StateTracker


# ── Fixtures ─────────────────────────────────────────────────────────────────

def make_check(name="build", conclusion="SUCCESS", status="COMPLETED") -> CheckRun:
    return CheckRun(name=name, conclusion=conclusion, status=status)


def make_pr(**overrides) -> PRData:
    defaults = dict(
        repo="RepoA", number=1, title="Test PR", branch="feat/KAN-10",
        url="https://github.com/Org/RepoA/pull/1",
        mergeable="MERGEABLE", merge_state_status="CLEAN",
        review_decision="", checks=[make_check()],
    )
    defaults.update(overrides)
    return PRData(**defaults)


@pytest.fixture
def config():
    return Config(
        org="TestOrg", repos=["RepoA"], slack_webhook_url="https://hooks.slack.com/test",
        poll_interval=1, cooldown=1800,
    )


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


# ── Prompts ──────────────────────────────────────────────────────────────────

class TestPrompts:
    def test_fix_all_with_conflicts(self):
        pr = make_pr(number=5, branch="feat/KAN-10", mergeable="CONFLICTING")
        prompt = Prompts.fix_all("Org", "RepoA", pr)
        assert "Merge Conflicts" in prompt
        assert "RepoA" in prompt
        assert "feat/KAN-10" in prompt
        assert "#5" in prompt

    def test_fix_all_with_ci_failures(self):
        pr = make_pr(checks=[make_check(name="test-suite", conclusion="FAILURE")])
        prompt = Prompts.fix_all("Org", "RepoA", pr)
        assert "CI Failures" in prompt
        assert "test-suite" in prompt

    def test_fix_all_with_review_comments_only(self):
        pr = make_pr(number=7, review_decision="CHANGES_REQUESTED")
        prompt = Prompts.fix_all("Org", "RepoA", pr)
        # Should contain the PR URL — either via loaded skill or fallback
        assert pr.url in prompt

    def test_fix_all_with_review_comments_no_ticket(self):
        pr = make_pr(number=7, branch="fix/no-ticket", review_decision="CHANGES_REQUESTED")
        prompt = Prompts.fix_all("Org", "RepoA", pr)
        assert pr.url in prompt

    def test_fix_all_includes_all_issues(self):
        pr = make_pr(
            number=3,
            mergeable="CONFLICTING",
            review_decision="CHANGES_REQUESTED",
            checks=[make_check(name="build", conclusion="FAILURE")],
        )
        prompt = Prompts.fix_all("Org", "RepoA", pr)
        assert "Merge Conflicts" in prompt
        assert "Review Comments" in prompt
        assert "CI Failures" in prompt
        assert "build" in prompt

    def test_fix_all_no_issues_still_has_verify(self):
        pr = make_pr(number=1)
        prompt = Prompts.fix_all("Org", "RepoA", pr)
        assert "Verify & Push" in prompt


# ── StateTracker ─────────────────────────────────────────────────────────────

class TestStateTracker:
    def test_get_creates_new_entry(self, db):
        st = StateTracker(db)
        tracked = st.get("RepoA", 1)
        assert tracked.repo == "RepoA"
        assert tracked.number == 1

    def test_get_returns_same_object(self, db):
        st = StateTracker(db)
        t1 = st.get("RepoA", 1)
        t2 = st.get("RepoA", 1)
        assert t1 is t2

    def test_save_and_reload(self, db):
        st = StateTracker(db)
        tracked = st.get("RepoA", 1)
        tracked.branch = "feat/test"
        tracked.last_issue = "ci_failing"
        tracked.claude_pid = 999
        st.save()

        st2 = StateTracker(db)
        loaded = st2.get("RepoA", 1)
        assert loaded.branch == "feat/test"
        assert loaded.last_issue == "ci_failing"
        assert loaded.claude_pid == 999

    def test_remove_closed(self, db):
        st = StateTracker(db)
        st.get("RepoA", 1)
        st.get("RepoA", 2)
        st.get("RepoB", 3)

        st.remove_closed({"RepoA#1", "RepoB#3"})
        assert "RepoA#2" not in st.prs
        assert "RepoA#1" in st.prs
        assert "RepoB#3" in st.prs

    def test_handles_empty_db(self, db):
        st = StateTracker(db)
        assert st.prs == {}

    def test_save_persists_booleans(self, db):
        st = StateTracker(db)
        tracked = st.get("RepoA", 1)
        tracked.ci_was_failing = True
        tracked.slack_sent = True
        st.save()

        st2 = StateTracker(db)
        loaded = st2.get("RepoA", 1)
        assert loaded.ci_was_failing is True
        assert loaded.slack_sent is True


# ── PRMonitorThread ──────────────────────────────────────────────────────────

class TestPRMonitorThread:
    def _make_monitor(self, config, db, **overrides):
        defaults = dict(
            config=config,
            gh=MagicMock(),
            terminal=MagicMock(),
            session_mgr=MagicMock(),
            db=db,
            ui_queue=queue.Queue(),
        )
        defaults.update(overrides)
        return PRMonitorThread(**defaults)

    def test_is_daemon_thread(self, config, db):
        monitor = self._make_monitor(config, db)
        assert monitor.daemon is True

    def test_stop_event(self, config, db):
        monitor = self._make_monitor(config, db)
        assert not monitor._stop_event.is_set()
        monitor.stop()
        assert monitor._stop_event.is_set()

    def test_poll_pushes_to_ui_queue(self, config, db):
        ui_q = queue.Queue()
        gh = MagicMock()
        gh.fetch_all_prs.return_value = [make_pr()]

        monitor = self._make_monitor(config, db, gh=gh, ui_queue=ui_q)
        monitor._poll()

        events = []
        while not ui_q.empty():
            events.append(ui_q.get_nowait())

        event_types = [e[0] for e in events]
        assert "update_prs" in event_types
        assert "poll_complete" in event_types

    def test_poll_updates_branch_for_behind(self, config, db):
        gh = MagicMock()
        pr = make_pr(merge_state_status="BEHIND")
        gh.fetch_all_prs.return_value = [pr]
        gh.update_branch.return_value = True

        monitor = self._make_monitor(config, db, gh=gh)
        monitor._poll()

        gh.update_branch.assert_called_once_with("RepoA", 1)

    def test_poll_does_not_auto_launch_claude(self, config, db):
        """Claude launches are now manual — monitor should NOT auto-launch."""
        gh = MagicMock()
        terminal = MagicMock()
        session_mgr = MagicMock()
        session_mgr.find_claude_session.return_value = None

        pr = make_pr(mergeable="CONFLICTING")
        gh.fetch_all_prs.return_value = [pr]

        monitor = self._make_monitor(config, db, gh=gh, terminal=terminal, session_mgr=session_mgr)
        monitor._poll()

        terminal.launch_pr_fix.assert_not_called()

    @patch("slack_client.send_webhook", return_value=True)
    def test_poll_sends_slack_for_ci_now_passing(self, mock_webhook, config, db):
        gh = MagicMock()

        pr = make_pr(checks=[make_check()])
        gh.fetch_all_prs.return_value = [pr]

        # Pre-seed state in DB
        db.execute(
            "INSERT INTO tracked_prs (key, repo, number, branch, last_issue, last_action, "
            "last_action_time, claude_pid, ci_was_failing, slack_sent) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("RepoA#1", "RepoA", 1, "b", "", "", 0.0, None, 1, 0),
        )

        monitor = self._make_monitor(config, db, gh=gh)
        monitor._poll()

        mock_webhook.assert_called_once()

    def test_poll_respects_cooldown(self, config, db):
        gh = MagicMock()
        terminal = MagicMock()
        session_mgr = MagicMock()
        session_mgr.find_claude_session.return_value = None

        pr = make_pr(checks=[make_check(conclusion="FAILURE")])
        gh.fetch_all_prs.return_value = [pr]

        # Pre-seed state with recent action
        db.execute(
            "INSERT INTO tracked_prs (key, repo, number, branch, last_issue, last_action, "
            "last_action_time, claude_pid, ci_was_failing, slack_sent) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("RepoA#1", "RepoA", 1, "b", "ci_failing", "launch_claude", time.time(), None, 0, 0),
        )

        monitor = self._make_monitor(config, db, gh=gh, terminal=terminal, session_mgr=session_mgr)
        monitor._poll()

        terminal.launch_pr_fix.assert_not_called()

    def test_poll_skips_if_claude_pid_alive(self, config, db):
        gh = MagicMock()
        terminal = MagicMock()
        session_mgr = MagicMock()

        pr = make_pr(checks=[make_check(conclusion="FAILURE")])
        gh.fetch_all_prs.return_value = [pr]

        # Pre-seed state with alive PID (our own PID)
        db.execute(
            "INSERT INTO tracked_prs (key, repo, number, branch, last_issue, last_action, "
            "last_action_time, claude_pid, ci_was_failing, slack_sent) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("RepoA#1", "RepoA", 1, "b", "", "", 0.0, os.getpid(), 0, 0),
        )

        monitor = self._make_monitor(config, db, gh=gh, terminal=terminal, session_mgr=session_mgr)
        monitor._poll()

        terminal.launch_pr_fix.assert_not_called()

    def test_poll_cleans_up_closed_prs(self, config, db):
        gh = MagicMock()
        gh.fetch_all_prs.return_value = []  # No open PRs

        db.execute(
            "INSERT INTO tracked_prs (key, repo, number, branch, last_issue, last_action, "
            "last_action_time, claude_pid, ci_was_failing, slack_sent) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("RepoA#1", "RepoA", 1, "b", "", "", 0.0, None, 0, 0),
        )

        monitor = self._make_monitor(config, db, gh=gh)
        monitor._poll()

        assert "RepoA#1" not in monitor.state.prs

    def test_poll_now_triggers_background_poll(self, config, db):
        gh = MagicMock()
        gh.fetch_all_prs.return_value = []

        monitor = self._make_monitor(config, db, gh=gh)

        with patch("pr_monitor.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            monitor.poll_now()
            mock_thread.assert_called_once()
            mock_thread.return_value.start.assert_called_once()

    def test_get_prompt_conflicts(self, config, db):
        monitor = self._make_monitor(config, db)
        pr = make_pr(mergeable="CONFLICTING")
        prompt = monitor._get_prompt(pr, PRIssueType.CONFLICTS)
        assert "Merge Conflicts" in prompt

    def test_get_prompt_ci_failing(self, config, db):
        monitor = self._make_monitor(config, db)
        pr = make_pr(checks=[make_check(conclusion="FAILURE")])
        prompt = monitor._get_prompt(pr, PRIssueType.CI_FAILING)
        assert "CI Failures" in prompt

    def test_get_prompt_changes_requested(self, config, db):
        monitor = self._make_monitor(config, db)
        pr = make_pr(review_decision="CHANGES_REQUESTED")
        prompt = monitor._get_prompt(pr, PRIssueType.CHANGES_REQUESTED)
        assert pr.url in prompt

    def test_get_prompt_always_includes_verify(self, config, db):
        monitor = self._make_monitor(config, db)
        prompt = monitor._get_prompt(make_pr(), PRIssueType.NONE)
        assert "Verify & Push" in prompt
