"""Data classes and enums for the Claude Management GUI."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional


# ──────────────────────────── Enums ──────────────────────────────────────────

class PRIssueType(Enum):
    BEHIND = "behind_main"
    CONFLICTS = "merge_conflicts"
    CI_FAILING = "ci_failing"
    CHANGES_REQUESTED = "changes_requested"
    CI_NOW_PASSING = "ci_now_passing"
    NONE = "none"


class PRAction(Enum):
    UPDATE_BRANCH = "update_branch"
    LAUNCH_CLAUDE = "launch_claude"
    SEND_SLACK = "send_slack"
    SKIP = "skip"


class PRStatus(Enum):
    PASSING = "passing"
    APPROVED = "approved"
    BEHIND = "behind"
    CONFLICTS = "conflicts"
    CI_FAILING = "ci_failing"
    CHANGES_REQUESTED = "changes_requested"
    PENDING = "pending"


class SessionStatus(Enum):
    RUNNING = "running"
    STOPPED = "stopped"


# ──────────────────────────── PR Data Classes ────────────────────────────────

IGNORED_CHECKS = {
    "notify / notify",
    "call-review / code-review",
    "sync / sync-jira",
}

TICKET_RE = re.compile(r"[A-Za-z]+-[0-9]+")


@dataclass
class CheckRun:
    name: str
    conclusion: Optional[str]
    status: str

    @classmethod
    def from_dict(cls, d: dict) -> CheckRun:
        return cls(
            name=d.get("name", ""),
            conclusion=d.get("conclusion", ""),
            status=d.get("status", ""),
        )


@dataclass
class PRData:
    repo: str
    number: int
    title: str
    branch: str
    url: str
    mergeable: str
    merge_state_status: str
    review_decision: str
    checks: list[CheckRun] = field(default_factory=list)
    unresolved_thread_count: int = 0
    is_draft: bool = False
    author: str = ""

    @property
    def relevant_checks(self) -> list[CheckRun]:
        return [c for c in self.checks if c.name not in IGNORED_CHECKS]

    @property
    def has_failing_checks(self) -> bool:
        return any(c.conclusion == "FAILURE" for c in self.relevant_checks)

    @property
    def all_checks_passed(self) -> bool:
        relevant = self.relevant_checks
        if not relevant:
            return True
        return all(
            c.conclusion == "SUCCESS" or (c.status == "COMPLETED" and c.conclusion != "FAILURE")
            for c in relevant
        )

    @property
    def checks_pending(self) -> bool:
        relevant = self.relevant_checks
        if not relevant:
            return False
        return any(c.status not in ("COMPLETED",) and c.conclusion in ("", None) for c in relevant)

    @property
    def ticket_id(self) -> Optional[str]:
        m = TICKET_RE.search(self.branch)
        return m.group(0) if m else None

    @property
    def status(self) -> PRStatus:
        if self.merge_state_status == "BEHIND":
            return PRStatus.BEHIND
        if self.mergeable == "CONFLICTING" or self.merge_state_status == "DIRTY":
            return PRStatus.CONFLICTS
        if self.has_failing_checks:
            return PRStatus.CI_FAILING
        if self.review_decision == "CHANGES_REQUESTED":
            return PRStatus.CHANGES_REQUESTED
        if self.all_checks_passed and self.review_decision == "APPROVED":
            return PRStatus.APPROVED
        if self.all_checks_passed:
            return PRStatus.PASSING
        return PRStatus.PENDING

    @property
    def issues(self) -> list[str]:
        """Return all active issues for this PR (not just highest priority)."""
        problems = []
        if self.merge_state_status == "BEHIND":
            problems.append("Behind")
        if self.mergeable == "CONFLICTING" or self.merge_state_status == "DIRTY":
            problems.append("Conflicts")
        if self.has_failing_checks:
            problems.append("CI Failing")
        if self.unresolved_thread_count > 0:
            problems.append(f"{self.unresolved_thread_count} Unresolved Comment{'s' if self.unresolved_thread_count != 1 else ''}")
        elif self.review_decision == "CHANGES_REQUESTED":
            problems.append("Changes Requested")
        return problems

    @property
    def is_ready_for_review(self) -> bool:
        """True only if CI passes and there are no issues blocking review."""
        return self.all_checks_passed and not self.issues

    @property
    def status_display(self) -> str:
        problems = self.issues
        if problems:
            return "\u274c " + ", ".join(problems)
        if self.all_checks_passed and self.review_decision == "APPROVED":
            return "\u2705 Approved"
        if self.all_checks_passed:
            return "\u2705 Passing"
        return "\u23f3 Pending"


@dataclass
class TrackedPR:
    repo: str
    number: int
    branch: str = ""
    last_issue: str = ""
    last_action: str = ""
    last_action_time: float = 0.0
    claude_pid: Optional[int] = None
    ci_was_failing: bool = False
    slack_sent: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> TrackedPR:
        return cls(
            repo=d.get("repo", ""),
            number=d.get("number", 0),
            branch=d.get("branch", ""),
            last_issue=d.get("last_issue", ""),
            last_action=d.get("last_action", ""),
            last_action_time=d.get("last_action_time", 0.0),
            claude_pid=d.get("claude_pid"),
            ci_was_failing=d.get("ci_was_failing", False),
            slack_sent=d.get("slack_sent", False),
        )


# ──────────────────────────── Session Data ───────────────────────────────────

@dataclass
class ManagedSession:
    name: str
    repo: str
    pid: Optional[int] = None
    session_id: Optional[str] = None
    status: SessionStatus = SessionStatus.RUNNING
    created_at: float = 0.0
    ticket_id: Optional[str] = None
    cwd: Optional[str] = None
    pr_url: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ManagedSession:
        return cls(
            name=d.get("name", ""),
            repo=d.get("repo", ""),
            pid=d.get("pid"),
            session_id=d.get("session_id"),
            status=SessionStatus(d.get("status", "stopped")),
            created_at=d.get("created_at", 0.0),
            ticket_id=d.get("ticket_id"),
            cwd=d.get("cwd"),
            pr_url=d.get("pr_url"),
        )


# ──────────────────────────── Helpers ────────────────────────────────────────

def is_pid_alive(pid: Optional[int]) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def extract_ticket_id(text: str) -> Optional[str]:
    m = TICKET_RE.search(text)
    return m.group(0) if m else None


def classify_pr(pr: PRData, tracked: TrackedPR) -> tuple[PRIssueType, PRAction]:
    """Classify the highest-priority issue for a PR."""
    if pr.mergeable == "UNKNOWN":
        return PRIssueType.NONE, PRAction.SKIP

    if pr.merge_state_status == "BEHIND":
        return PRIssueType.BEHIND, PRAction.UPDATE_BRANCH

    if pr.mergeable == "CONFLICTING" or pr.merge_state_status == "DIRTY":
        return PRIssueType.CONFLICTS, PRAction.LAUNCH_CLAUDE

    if pr.has_failing_checks:
        return PRIssueType.CI_FAILING, PRAction.LAUNCH_CLAUDE

    if pr.review_decision == "CHANGES_REQUESTED":
        return PRIssueType.CHANGES_REQUESTED, PRAction.LAUNCH_CLAUDE

    if tracked.ci_was_failing and pr.all_checks_passed and not tracked.slack_sent:
        return PRIssueType.CI_NOW_PASSING, PRAction.SEND_SLACK

    return PRIssueType.NONE, PRAction.SKIP
