"""Tests for models.py — data classes, enums, classify_pr, helpers."""

import os
from unittest.mock import patch

import pytest

from models import (
    IGNORED_CHECKS,
    CheckRun,
    ManagedSession,
    PRAction,
    PRData,
    PRIssueType,
    PRStatus,
    SessionStatus,
    TrackedPR,
    classify_pr,
    extract_ticket_id,
    is_pid_alive,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def make_check(name="build", conclusion="SUCCESS", status="COMPLETED") -> CheckRun:
    return CheckRun(name=name, conclusion=conclusion, status=status)


def make_pr(**overrides) -> PRData:
    defaults = dict(
        repo="TestRepo",
        number=1,
        title="Test PR",
        branch="feat/KAN-42-feature",
        url="https://github.com/Org/TestRepo/pull/1",
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision="",
        checks=[make_check()],
    )
    defaults.update(overrides)
    return PRData(**defaults)


def make_tracked(**overrides) -> TrackedPR:
    defaults = dict(repo="TestRepo", number=1, branch="feat/KAN-42-feature")
    defaults.update(overrides)
    return TrackedPR(**defaults)


# ── CheckRun ─────────────────────────────────────────────────────────────────

class TestCheckRun:
    def test_from_dict(self):
        cr = CheckRun.from_dict({"name": "build", "conclusion": "SUCCESS", "status": "COMPLETED"})
        assert cr.name == "build"
        assert cr.conclusion == "SUCCESS"
        assert cr.status == "COMPLETED"

    def test_from_dict_missing_fields(self):
        cr = CheckRun.from_dict({})
        assert cr.name == ""
        assert cr.conclusion == ""
        assert cr.status == ""


# ── PRData Properties ────────────────────────────────────────────────────────

class TestPRDataProperties:
    def test_relevant_checks_filters_ignored(self):
        checks = [
            make_check(name="build"),
            make_check(name="notify / notify"),
            make_check(name="call-review / code-review"),
            make_check(name="sync / sync-jira"),
        ]
        pr = make_pr(checks=checks)
        assert len(pr.relevant_checks) == 1
        assert pr.relevant_checks[0].name == "build"

    def test_has_failing_checks_true(self):
        pr = make_pr(checks=[make_check(conclusion="FAILURE")])
        assert pr.has_failing_checks is True

    def test_has_failing_checks_false(self):
        pr = make_pr(checks=[make_check(conclusion="SUCCESS")])
        assert pr.has_failing_checks is False

    def test_has_failing_checks_ignores_ignored_checks(self):
        pr = make_pr(checks=[make_check(name="notify / notify", conclusion="FAILURE")])
        assert pr.has_failing_checks is False

    def test_all_checks_passed_true(self):
        pr = make_pr(checks=[
            make_check(conclusion="SUCCESS"),
            make_check(conclusion="SUCCESS"),
        ])
        assert pr.all_checks_passed is True

    def test_all_checks_passed_false_when_failing(self):
        pr = make_pr(checks=[
            make_check(conclusion="SUCCESS"),
            make_check(conclusion="FAILURE"),
        ])
        assert pr.all_checks_passed is False

    def test_all_checks_passed_true_when_no_relevant_checks(self):
        pr = make_pr(checks=[])
        assert pr.all_checks_passed is True

    def test_checks_pending_true(self):
        pr = make_pr(checks=[make_check(status="IN_PROGRESS", conclusion="")])
        assert pr.checks_pending is True

    def test_checks_pending_false_when_all_completed(self):
        pr = make_pr(checks=[make_check(status="COMPLETED", conclusion="SUCCESS")])
        assert pr.checks_pending is False

    def test_checks_pending_false_when_no_checks(self):
        pr = make_pr(checks=[])
        assert pr.checks_pending is False

    def test_ticket_id_from_branch(self):
        pr = make_pr(branch="feat/KAN-42-some-feature")
        assert pr.ticket_id == "KAN-42"

    def test_ticket_id_none_when_no_match(self):
        pr = make_pr(branch="feat/no-ticket-here")
        assert pr.ticket_id is None

    def test_ticket_id_various_formats(self):
        assert make_pr(branch="PROJ-123-foo").ticket_id == "PROJ-123"
        assert make_pr(branch="fix/ABC-1").ticket_id == "ABC-1"


# ── PRData.issues / is_ready_for_review ──────────────────────────────────────

class TestPRIssues:
    def test_no_issues_when_clean(self):
        pr = make_pr(checks=[make_check()])
        assert pr.issues == []

    def test_behind_issue(self):
        pr = make_pr(merge_state_status="BEHIND")
        assert "Behind" in pr.issues

    def test_conflicts_issue(self):
        pr = make_pr(mergeable="CONFLICTING")
        assert "Conflicts" in pr.issues

    def test_ci_failing_issue(self):
        pr = make_pr(checks=[make_check(conclusion="FAILURE")])
        assert "CI Failing" in pr.issues

    def test_unresolved_threads_issue(self):
        pr = make_pr(unresolved_thread_count=2)
        issues = pr.issues
        assert any("2 Unresolved Comments" in i for i in issues)

    def test_unresolved_threads_singular(self):
        pr = make_pr(unresolved_thread_count=1)
        issues = pr.issues
        assert any("1 Unresolved Comment" in i for i in issues)
        assert not any("Comments" in i for i in issues)

    def test_changes_requested_review_decision_ignored(self):
        """CHANGES_REQUESTED review decision alone does not count as an issue."""
        pr = make_pr(review_decision="CHANGES_REQUESTED")
        assert pr.issues == []

    def test_threads_take_precedence_over_review_decision(self):
        """unresolved_thread_count is shown instead of review_decision."""
        pr = make_pr(review_decision="CHANGES_REQUESTED", unresolved_thread_count=3)
        issues = pr.issues
        assert any("3 Unresolved Comments" in i for i in issues)
        assert "Changes Requested" not in issues

    def test_multiple_issues(self):
        pr = make_pr(
            merge_state_status="BEHIND",
            mergeable="CONFLICTING",
            checks=[make_check(conclusion="FAILURE")],
            unresolved_thread_count=2,
        )
        assert len(pr.issues) == 4

    def test_is_ready_for_review_true(self):
        pr = make_pr(checks=[make_check()])
        assert pr.is_ready_for_review is True

    def test_is_ready_for_review_false_with_issues(self):
        pr = make_pr(merge_state_status="BEHIND", checks=[make_check()])
        assert pr.is_ready_for_review is False

    def test_is_ready_for_review_true_when_no_checks(self):
        pr = make_pr(checks=[])
        assert pr.is_ready_for_review is True

    def test_is_ready_for_review_true_with_changes_requested(self):
        """CHANGES_REQUESTED review decision alone does not block readiness."""
        pr = make_pr(checks=[make_check()], review_decision="CHANGES_REQUESTED")
        assert pr.is_ready_for_review is True

    def test_is_ready_for_review_false_with_unresolved_threads(self):
        pr = make_pr(checks=[make_check()], unresolved_thread_count=1)
        assert pr.is_ready_for_review is False


# ── PRData.status ────────────────────────────────────────────────────────────

class TestPRStatus:
    def test_status_behind(self):
        pr = make_pr(merge_state_status="BEHIND")
        assert pr.status == PRStatus.BEHIND

    def test_status_conflicts_from_mergeable(self):
        pr = make_pr(mergeable="CONFLICTING")
        assert pr.status == PRStatus.CONFLICTS

    def test_status_conflicts_from_dirty(self):
        pr = make_pr(merge_state_status="DIRTY")
        assert pr.status == PRStatus.CONFLICTS

    def test_status_ci_failing(self):
        pr = make_pr(checks=[make_check(conclusion="FAILURE")])
        assert pr.status == PRStatus.CI_FAILING

    def test_status_changes_requested_only_from_threads(self):
        """CHANGES_REQUESTED status requires unresolved threads, not just review decision."""
        pr = make_pr(review_decision="CHANGES_REQUESTED")
        assert pr.status == PRStatus.PASSING
        pr2 = make_pr(review_decision="CHANGES_REQUESTED", unresolved_thread_count=1)
        assert pr2.status == PRStatus.CHANGES_REQUESTED

    def test_status_passing(self):
        pr = make_pr(checks=[make_check(conclusion="SUCCESS")])
        assert pr.status == PRStatus.PASSING

    def test_status_passing_no_checks(self):
        pr = make_pr(checks=[])
        assert pr.status == PRStatus.PASSING

    def test_status_display_passing(self):
        assert "Passing" in make_pr(checks=[make_check()]).status_display

    def test_status_display_shows_all_issues(self):
        pr = make_pr(
            merge_state_status="BEHIND",
            checks=[make_check(conclusion="FAILURE")],
            unresolved_thread_count=2,
        )
        display = pr.status_display
        assert "Behind" in display
        assert "CI Failing" in display
        assert "2 Unresolved Comments" in display

    def test_status_display_single_issue(self):
        assert "Conflicts" in make_pr(mergeable="CONFLICTING").status_display
        assert "CI Failing" in make_pr(checks=[make_check(conclusion="FAILURE")]).status_display

    def test_status_priority_behind_beats_ci(self):
        """BEHIND takes priority even if checks are failing."""
        pr = make_pr(
            merge_state_status="BEHIND",
            checks=[make_check(conclusion="FAILURE")],
        )
        assert pr.status == PRStatus.BEHIND


# ── classify_pr ──────────────────────────────────────────────────────────────

class TestClassifyPR:
    def test_skip_when_mergeable_unknown(self):
        pr = make_pr(mergeable="UNKNOWN")
        issue, action = classify_pr(pr, make_tracked())
        assert issue == PRIssueType.NONE
        assert action == PRAction.SKIP

    def test_behind_triggers_update_branch(self):
        pr = make_pr(merge_state_status="BEHIND")
        issue, action = classify_pr(pr, make_tracked())
        assert issue == PRIssueType.BEHIND
        assert action == PRAction.UPDATE_BRANCH

    def test_conflicts_triggers_claude(self):
        pr = make_pr(mergeable="CONFLICTING")
        issue, action = classify_pr(pr, make_tracked())
        assert issue == PRIssueType.CONFLICTS
        assert action == PRAction.LAUNCH_CLAUDE

    def test_dirty_triggers_claude(self):
        pr = make_pr(merge_state_status="DIRTY")
        issue, action = classify_pr(pr, make_tracked())
        assert issue == PRIssueType.CONFLICTS
        assert action == PRAction.LAUNCH_CLAUDE

    def test_ci_failing_triggers_claude(self):
        pr = make_pr(checks=[make_check(conclusion="FAILURE")])
        issue, action = classify_pr(pr, make_tracked())
        assert issue == PRIssueType.CI_FAILING
        assert action == PRAction.LAUNCH_CLAUDE

    def test_changes_requested_triggers_claude(self):
        pr = make_pr(review_decision="CHANGES_REQUESTED", unresolved_thread_count=2)
        issue, action = classify_pr(pr, make_tracked())
        assert issue == PRIssueType.CHANGES_REQUESTED
        assert action == PRAction.LAUNCH_CLAUDE

    def test_unresolved_threads_triggers_claude_without_changes_requested(self):
        pr = make_pr(review_decision="REVIEW_REQUIRED", unresolved_thread_count=4)
        issue, action = classify_pr(pr, make_tracked())
        assert issue == PRIssueType.CHANGES_REQUESTED
        assert action == PRAction.LAUNCH_CLAUDE

    def test_ci_now_passing_triggers_slack(self):
        pr = make_pr(checks=[make_check(conclusion="SUCCESS")])
        tracked = make_tracked(ci_was_failing=True, slack_sent=False)
        issue, action = classify_pr(pr, tracked)
        assert issue == PRIssueType.CI_NOW_PASSING
        assert action == PRAction.SEND_SLACK

    def test_ci_now_passing_skips_if_slack_sent(self):
        pr = make_pr(checks=[make_check(conclusion="SUCCESS")])
        tracked = make_tracked(ci_was_failing=True, slack_sent=True)
        issue, action = classify_pr(pr, tracked)
        assert issue == PRIssueType.NONE
        assert action == PRAction.SKIP

    def test_clean_pr_skips(self):
        pr = make_pr()
        issue, action = classify_pr(pr, make_tracked())
        assert issue == PRIssueType.NONE
        assert action == PRAction.SKIP

    def test_priority_behind_over_conflicts(self):
        """BEHIND should take priority even if there are also conflicts."""
        pr = make_pr(merge_state_status="BEHIND", mergeable="CONFLICTING")
        issue, action = classify_pr(pr, make_tracked())
        assert issue == PRIssueType.BEHIND

    def test_priority_conflicts_over_ci(self):
        pr = make_pr(
            mergeable="CONFLICTING",
            checks=[make_check(conclusion="FAILURE")],
        )
        issue, action = classify_pr(pr, make_tracked())
        assert issue == PRIssueType.CONFLICTS

    def test_priority_ci_over_changes_requested(self):
        pr = make_pr(
            checks=[make_check(conclusion="FAILURE")],
            review_decision="CHANGES_REQUESTED",
        )
        issue, action = classify_pr(pr, make_tracked())
        assert issue == PRIssueType.CI_FAILING


# ── TrackedPR Serialization ──────────────────────────────────────────────────

class TestTrackedPR:
    def test_roundtrip(self):
        t = TrackedPR(
            repo="Ats", number=5, branch="fix/KAN-10",
            last_issue="ci_failing", last_action="launch_claude",
            last_action_time=1000.0, claude_pid=42,
            ci_was_failing=True, slack_sent=False,
        )
        d = t.to_dict()
        t2 = TrackedPR.from_dict(d)
        assert t2.repo == "Ats"
        assert t2.number == 5
        assert t2.claude_pid == 42
        assert t2.ci_was_failing is True

    def test_from_dict_defaults(self):
        t = TrackedPR.from_dict({})
        assert t.repo == ""
        assert t.number == 0
        assert t.claude_pid is None
        assert t.ci_was_failing is False


# ── ManagedSession Serialization ─────────────────────────────────────────────

class TestManagedSession:
    def test_roundtrip(self):
        s = ManagedSession(
            name="session-1", repo="Ats", pid=100,
            session_id="abc-123", status=SessionStatus.RUNNING,
            created_at=999.0, ticket_id="KAN-5",
            cwd="/some/path",
        )
        d = s.to_dict()
        assert d["status"] == "running"
        assert d["cwd"] == "/some/path"
        s2 = ManagedSession.from_dict(d)
        assert s2.name == "session-1"
        assert s2.status == SessionStatus.RUNNING
        assert s2.ticket_id == "KAN-5"
        assert s2.cwd == "/some/path"

    def test_from_dict_defaults(self):
        s = ManagedSession.from_dict({})
        assert s.name == ""
        assert s.status == SessionStatus.STOPPED
        assert s.pid is None
        assert s.cwd is None

    def test_cwd_none_by_default(self):
        s = ManagedSession(name="test", repo="Repo")
        assert s.cwd is None
        d = s.to_dict()
        assert d["cwd"] is None
        s2 = ManagedSession.from_dict(d)
        assert s2.cwd is None


# ── Helper Functions ─────────────────────────────────────────────────────────

class TestIsPidAlive:
    def test_none_returns_false(self):
        assert is_pid_alive(None) is False

    def test_own_pid_returns_true(self):
        assert is_pid_alive(os.getpid()) is True

    def test_nonexistent_pid_returns_false(self):
        assert is_pid_alive(99999999) is False


class TestExtractTicketId:
    def test_extracts_ticket(self):
        assert extract_ticket_id("feat/KAN-42-some-feature") == "KAN-42"

    def test_returns_none(self):
        assert extract_ticket_id("no-ticket") is None

    def test_first_match(self):
        assert extract_ticket_id("KAN-1/KAN-2") == "KAN-1"

    def test_case_insensitive_pattern(self):
        assert extract_ticket_id("abc-123") == "abc-123"
