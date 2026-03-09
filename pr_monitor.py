"""Background PR monitor thread."""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Optional

from config import Config
from database import Database
from github_client import GitHubClient
from models import (
    PRAction, PRData, PRIssueType, TrackedPR,
    classify_pr, is_pid_alive,
)
from session_manager import SessionManager
from skill_runner import _version_key
from terminal import TerminalLauncher

log = logging.getLogger("claude_mgmt")


# ──────────────────────────── Prompts ────────────────────────────────────────

class Prompts:
    _SKILL_CACHE: dict[str, str] = {}

    @staticmethod
    def _load_skill(name: str) -> str | None:
        """Load a plugin skill's SKILL.md content, cached after first read.

        Scans all installed plugins under ~/.claude/plugins/cache/ for a
        matching skill by name.
        """
        if name in Prompts._SKILL_CACHE:
            return Prompts._SKILL_CACHE[name]
        cache_root = os.path.expanduser("~/.claude/plugins/cache")
        if not os.path.isdir(cache_root):
            return None
        # Walk org/plugin directories looking for the skill
        for org in os.listdir(cache_root):
            org_path = os.path.join(cache_root, org)
            if not os.path.isdir(org_path):
                continue
            for plugin in os.listdir(org_path):
                plugin_path = os.path.join(org_path, plugin)
                if not os.path.isdir(plugin_path):
                    continue
                versions = [d for d in os.listdir(plugin_path)
                            if os.path.isdir(os.path.join(plugin_path, d))]
                if not versions:
                    continue
                latest = sorted(versions, key=_version_key)[-1]
                md_path = os.path.join(plugin_path, latest, "skills", name, "SKILL.md")
                if os.path.isfile(md_path):
                    try:
                        with open(md_path) as f:
                            content = f.read()
                        Prompts._SKILL_CACHE[name] = content
                        return content
                    except OSError:
                        continue
        return None

    @staticmethod
    def fix_all(org: str, repo: str, pr: PRData) -> str:
        """Build a single prompt that addresses every detected issue on the PR."""
        has_conflicts = (
            pr.mergeable == "CONFLICTING"
            or pr.merge_state_status == "DIRTY"
            or pr.merge_state_status == "BEHIND"
        )
        failing = [c.name for c in pr.relevant_checks if c.conclusion == "FAILURE"]
        has_ci_failures = bool(failing)
        has_review_comments = (
            pr.review_decision == "CHANGES_REQUESTED"
            or pr.unresolved_thread_count > 0
        )

        # If review comments are the only issue,
        # load and use the address-review skill template directly
        if has_review_comments and not has_conflicts and not has_ci_failures:
            skill = Prompts._load_skill("address-review")
            if skill:
                return skill.replace("$ARGUMENTS", pr.url)
            return f"Address all review comments on PR {pr.url}. Resolve any comment threads once they are fixed."

        lines: list[str] = [
            f"Fix ALL issues on PR #{pr.number} in {org}/{repo} on branch `{pr.branch}`.\n"
            f"Start by checking out the branch: git fetch origin && git checkout {pr.branch}\n"
        ]

        step = 1

        # ── Merge conflicts ─────────────────────────────────────────────
        if has_conflicts:
            lines.append(
                f"\n## {step}. Merge Conflicts\n"
                f"The branch has merge conflicts or is behind main.\n"
                f"- git merge origin/main and resolve all conflicts\n"
            )
            step += 1

        # ── Review comments ──────────────────────────────────────────────
        if has_review_comments:
            skill = Prompts._load_skill("address-review")
            if skill:
                lines.append(
                    f"\n## {step}. Review Comments\n"
                    + skill.replace("$ARGUMENTS", pr.url) + "\n"
                )
            else:
                lines.append(
                    f"\n## {step}. Review Comments\n"
                    f"Address all review comments on PR {pr.url}\n"
                )
            step += 1

        # ── CI failures ──────────────────────────────────────────────────
        if has_ci_failures:
            checks_str = ", ".join(failing)
            lines.append(
                f"\n## {step}. CI Failures\n"
                f"Failing checks: {checks_str}\n"
                f"- Investigate the CI logs: gh pr checks {pr.number} --repo {org}/{repo}\n"
                f"- Read the failing check logs to understand the failure\n"
                f"- Fix the issues\n"
            )
            step += 1

        # ── Final steps ──────────────────────────────────────────────────
        lines.append(
            f"\n## {step}. Verify & Push\n"
            f"- Build and run tests locally to verify everything works\n"
            f"- git push\n"
            f"- Wait for CI: gh pr checks {pr.number} --repo {org}/{repo} --watch\n"
            f"- Resolve any review comment threads that have been addressed by your changes"
        )

        return "".join(lines)


# ──────────────────────────── State Tracker ──────────────────────────────────

class StateTracker:
    def __init__(self, db: Database):
        self._db = db
        self._lock = threading.Lock()
        self.prs: dict[str, TrackedPR] = {}
        self._load()

    def _load(self):
        rows = self._db.fetchall("SELECT * FROM tracked_prs")
        for r in rows:
            self.prs[r["key"]] = TrackedPR(
                repo=r["repo"],
                number=r["number"],
                branch=r["branch"],
                last_issue=r["last_issue"],
                last_action=r["last_action"],
                last_action_time=r["last_action_time"],
                claude_pid=r["claude_pid"],
                ci_was_failing=bool(r["ci_was_failing"]),
                slack_sent=bool(r["slack_sent"]),
                watched=bool(r["watched"]),
            )

    def save(self):
        with self._lock:
            for key, pr in self.prs.items():
                self._db.execute(
                    "INSERT OR REPLACE INTO tracked_prs "
                    "(key, repo, number, branch, last_issue, last_action, "
                    "last_action_time, claude_pid, ci_was_failing, slack_sent, watched) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        pr.repo,
                        pr.number,
                        pr.branch,
                        pr.last_issue,
                        pr.last_action,
                        pr.last_action_time,
                        pr.claude_pid,
                        int(pr.ci_was_failing),
                        int(pr.slack_sent),
                        int(pr.watched),
                    ),
                )

    def get(self, repo: str, number: int) -> TrackedPR:
        key = f"{repo}#{number}"
        if key not in self.prs:
            self.prs[key] = TrackedPR(repo=repo, number=number, branch="")
        return self.prs[key]

    def set_watched(self, repo: str, number: int, watched: bool):
        key = f"{repo}#{number}"
        if key in self.prs:
            self.prs[key].watched = watched
            self.save()

    def get_watched_keys(self) -> set[str]:
        return {k for k, pr in self.prs.items() if pr.watched}

    def remove_closed(self, open_keys: set[str]):
        closed = [k for k in self.prs if k not in open_keys]
        for k in closed:
            del self.prs[k]
            self._db.execute("DELETE FROM tracked_prs WHERE key = ?", (k,))


# ──────────────────────────── Monitor Thread ─────────────────────────────────

class PRMonitorThread(threading.Thread):
    def __init__(
        self,
        config: Config,
        gh: GitHubClient,
        terminal: TerminalLauncher,
        session_mgr: SessionManager,
        db: Database,
        ui_queue: queue.Queue,
    ):
        super().__init__(daemon=True, name="PRMonitor")
        self.config = config
        self.gh = gh
        self.terminal = terminal
        self.session_mgr = session_mgr
        self.state = StateTracker(db)
        self.ui_queue = ui_queue
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        log.info("PR Monitor thread started (interval=%ds)", self.config.poll_interval)
        while not self._stop_event.is_set():
            try:
                self._poll()
            except Exception:
                log.exception("Error during PR monitor poll")
            self._stop_event.wait(self.config.poll_interval)

    def poll_now(self):
        """Trigger an immediate poll from another thread."""
        threading.Thread(target=self._poll, daemon=True, name="PRPoll").start()

    def _poll(self):
        log.info("PR poll cycle starting")
        prs = self.gh.fetch_all_prs()

        # Also fetch manually-added PRs
        manual_rows = self.state._db.fetchall("SELECT repo, number FROM manual_prs")
        existing_keys = {f"{pr.repo}#{pr.number}" for pr in prs}
        for row in manual_rows:
            key = f"{row['repo']}#{row['number']}"
            if key in existing_keys:
                continue  # Already fetched via --author @me
            pr = self.gh.fetch_single_pr(row["repo"], row["number"])
            if pr:
                prs.append(pr)
            else:
                # PR closed or not found — remove from manual list
                self.state._db.execute("DELETE FROM manual_prs WHERE key = ?", (key,))
                log.info("Removed closed manual PR %s", key)

        open_keys: set[str] = set()

        for pr in prs:
            key = f"{pr.repo}#{pr.number}"
            open_keys.add(key)
            tracked = self.state.get(pr.repo, pr.number)
            tracked.branch = pr.branch

            issue, action = classify_pr(pr, tracked)

            if pr.has_failing_checks:
                tracked.ci_was_failing = True

            if issue == PRIssueType.NONE:
                continue

            log.info("PR %s#%d (%s): issue=%s action=%s",
                     pr.repo, pr.number, pr.branch, issue.value, action.value)

            if action == PRAction.LAUNCH_CLAUDE:
                if not tracked.watched:
                    log.info("PR %s#%d needs fix (not watched, skipping)", pr.repo, pr.number)
                    continue
                # Don't launch if Claude is already running for this PR
                if tracked.claude_pid and is_pid_alive(tracked.claude_pid):
                    log.info("PR %s#%d already has Claude running (pid %d)", pr.repo, pr.number, tracked.claude_pid)
                    continue
                self.ui_queue.put(("auto_launch_fix", pr))
                tracked.last_issue = issue.value
                tracked.last_action = action.value
                tracked.last_action_time = time.time()
                continue

            self._execute(pr, tracked, issue, action)

        self.state.remove_closed(open_keys)
        self.state.save()

        self.ui_queue.put(("update_prs", prs))
        self.ui_queue.put(("poll_complete", time.time()))
        log.info("PR poll cycle complete — %d open PRs", len(prs))

    def _execute(self, pr: PRData, tracked: TrackedPR, issue: PRIssueType, action: PRAction):
        if action == PRAction.UPDATE_BRANCH:
            success = self.gh.update_branch(pr.repo, pr.number)
            if success:
                tracked.last_issue = issue.value
                tracked.last_action = action.value
                tracked.last_action_time = time.time()

        elif action == PRAction.LAUNCH_CLAUDE:
            prompt = self._get_prompt(pr, issue)
            session_id = self.session_mgr.find_claude_session(pr.repo, pr.ticket_id)
            pid = self.terminal.launch_pr_fix(pr.repo, pr.number, pr.branch, prompt, session_id)
            tracked.claude_pid = pid
            tracked.last_issue = issue.value
            tracked.last_action = action.value
            tracked.last_action_time = time.time()

        elif action == PRAction.SEND_SLACK:
            msg = f"PR {pr.url} ({pr.repo}#{pr.number}) — CI is now passing. Ready for review."
            slack_mode = self.config.slack_mode
            if slack_mode == "mcp" and self.config.slack_channel:
                from slack_client import send_via_mcp
                success = send_via_mcp(
                    self.config.slack_channel, msg,
                    oauth_token=self.config.claude_oauth_token,
                    cwd=self.config.base_dir,
                )
            elif self.config.slack_webhook_url:
                from slack_client import send_webhook
                success = send_webhook(self.config.slack_webhook_url, msg)
            else:
                success = False
                log.warning("No Slack configuration found, skipping notification for %s#%d",
                            pr.repo, pr.number)
            if success:
                tracked.slack_sent = True
                tracked.last_issue = issue.value
                tracked.last_action = action.value
                tracked.last_action_time = time.time()

    def _get_prompt(self, pr: PRData, issue: PRIssueType) -> str:
        org = self.config.org
        return Prompts.fix_all(org, pr.repo, pr)
