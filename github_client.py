"""GitHub CLI wrapper for PR fetching and branch updates."""

from __future__ import annotations

import json
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import Config
from models import CheckRun, PRData

log = logging.getLogger("claude_mgmt")

PR_FIELDS = ",".join([
    "number", "title", "headRefName", "url",
    "mergeable", "mergeStateStatus",
    "statusCheckRollup", "reviewDecision",
    "state", "isDraft", "author", "headRefOid",
])



class GitHubClient:
    def __init__(self, config: Config):
        self.config = config
        self._current_user: str | None = None

    def fetch_prs(self, repo: str) -> list[PRData]:
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "list",
                    "--repo", f"{self.config.org}/{repo}",
                    "--author", "@me",
                    "--state", "open",
                    "--json", PR_FIELDS,
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                log.error("gh pr list failed for %s: %s", repo, result.stderr.strip())
                return []
            prs_json = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            log.error("Failed to fetch PRs for %s: %s", repo, e)
            return []

        prs = []
        pr_numbers = []
        for p in prs_json:
            checks = [CheckRun.from_dict(c) for c in (p.get("statusCheckRollup") or [])]
            prs.append(PRData(
                repo=repo,
                number=p["number"],
                title=p.get("title", ""),
                branch=p.get("headRefName", ""),
                url=p.get("url", ""),
                mergeable=p.get("mergeable", "UNKNOWN"),
                merge_state_status=p.get("mergeStateStatus", "UNKNOWN"),
                review_decision=p.get("reviewDecision", "") or "",
                checks=checks,
                is_draft=p.get("isDraft", False),
                author=p.get("author", {}).get("login", ""),
                head_sha=p.get("headRefOid", ""),
            ))
            pr_numbers.append(p["number"])

        # Fetch unresolved review thread counts
        if pr_numbers:
            thread_counts = self._fetch_unresolved_threads(repo, pr_numbers)
            for pr in prs:
                pr.unresolved_thread_count = thread_counts.get(pr.number, 0)

        return prs

    def _fetch_unresolved_threads(self, repo: str, pr_numbers: list[int]) -> dict[int, int]:
        """Query GitHub GraphQL for unresolved review thread counts per PR."""
        # Build aliased query fragments for each PR
        fragments = []
        for num in pr_numbers:
            fragments.append(
                f'pr{num}: pullRequest(number: {num}) {{'
                f'  reviewThreads(first: 100) {{ nodes {{ isResolved }} }}'
                f'}}'
            )
        query = (
            f'query {{ repository(owner: "{self.config.org}", name: "{repo}") {{'
            + " ".join(fragments)
            + '}}'
        )

        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={query}"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                log.error("GraphQL review threads failed for %s: %s", repo, result.stderr.strip())
                return {}
            data = json.loads(result.stdout).get("data", {}).get("repository", {})
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            log.error("Failed to fetch review threads for %s: %s", repo, e)
            return {}

        counts: dict[int, int] = {}
        for num in pr_numbers:
            pr_data = data.get(f"pr{num}", {})
            threads = pr_data.get("reviewThreads", {}).get("nodes", [])
            counts[num] = sum(1 for t in threads if not t.get("isResolved", True))
        return counts

    def fetch_all_prs(self) -> list[PRData]:
        all_prs: list[PRData] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(self.fetch_prs, repo): repo for repo in self.config.repos}
            for future in as_completed(futures):
                try:
                    all_prs.extend(future.result())
                except Exception as e:
                    log.error("Error fetching PRs for %s: %s", futures[future], e)
        return all_prs

    def update_branch(self, repo: str, number: int) -> bool:
        try:
            result = subprocess.run(
                [
                    "gh", "api",
                    f"repos/{self.config.org}/{repo}/pulls/{number}/update-branch",
                    "-X", "PUT",
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                log.info("Updated branch for %s#%d", repo, number)
                return True
            log.error("Branch update failed for %s#%d: %s", repo, number, result.stderr.strip())
            return False
        except (subprocess.TimeoutExpired, OSError) as e:
            log.error("Branch update error for %s#%d: %s", repo, number, e)
            return False

    def merge_pr(self, repo: str, number: int) -> bool:
        """Merge a PR using squash merge via gh CLI."""
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "merge", str(number),
                    "--repo", f"{self.config.org}/{repo}",
                    "--squash",
                    "--delete-branch",
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                log.info("Merged %s#%d", repo, number)
                return True
            log.error("Merge failed for %s#%d: %s", repo, number, result.stderr.strip())
            return False
        except (subprocess.TimeoutExpired, OSError) as e:
            log.error("Merge error for %s#%d: %s", repo, number, e)
            return False

    def mark_ready_for_review(self, repo: str, number: int) -> bool:
        """Remove draft status from a PR using the GraphQL API."""
        # First get the PR node ID
        try:
            result = subprocess.run(
                [
                    "gh", "api",
                    f"repos/{self.config.org}/{repo}/pulls/{number}",
                    "--jq", ".node_id",
                ],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0 or not result.stdout.strip():
                log.error("Failed to get node_id for %s#%d: %s", repo, number, result.stderr.strip())
                return False
            node_id = result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError) as e:
            log.error("Failed to get node_id for %s#%d: %s", repo, number, e)
            return False

        query = (
            'mutation { markPullRequestReadyForReview'
            f'(input: {{ pullRequestId: "{node_id}" }}) '
            '{ pullRequest { isDraft } } }'
        )
        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={query}"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                log.info("Marked %s#%d as ready for review", repo, number)
                return True
            log.error("Failed to mark %s#%d ready: %s", repo, number, result.stderr.strip())
            return False
        except (subprocess.TimeoutExpired, OSError) as e:
            log.error("Failed to mark %s#%d ready: %s", repo, number, e)
            return False

    def get_current_user(self) -> str:
        """Return the GitHub login of the authenticated user (cached)."""
        if self._current_user:
            return self._current_user
        try:
            result = subprocess.run(
                ["gh", "api", "user", "--jq", ".login"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                self._current_user = result.stdout.strip()
                return self._current_user
        except (subprocess.TimeoutExpired, OSError) as e:
            log.error("Failed to get current user: %s", e)
        return ""

    def fetch_team_prs(self, repo: str) -> list[PRData]:
        """Fetch all open PRs for a repo (not filtered by author)."""
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "list",
                    "--repo", f"{self.config.org}/{repo}",
                    "--state", "open",
                    "--json", PR_FIELDS,
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                log.error("gh pr list (team) failed for %s: %s", repo, result.stderr.strip())
                return []
            prs_json = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            log.error("Failed to fetch team PRs for %s: %s", repo, e)
            return []

        prs = []
        pr_numbers = []
        for p in prs_json:
            checks = [CheckRun.from_dict(c) for c in (p.get("statusCheckRollup") or [])]
            prs.append(PRData(
                repo=repo,
                number=p["number"],
                title=p.get("title", ""),
                branch=p.get("headRefName", ""),
                url=p.get("url", ""),
                mergeable=p.get("mergeable", "UNKNOWN"),
                merge_state_status=p.get("mergeStateStatus", "UNKNOWN"),
                review_decision=p.get("reviewDecision", "") or "",
                checks=checks,
                is_draft=p.get("isDraft", False),
                author=p.get("author", {}).get("login", ""),
                head_sha=p.get("headRefOid", ""),
            ))
            pr_numbers.append(p["number"])

        if pr_numbers:
            thread_counts = self._fetch_unresolved_threads(repo, pr_numbers)
            for pr in prs:
                pr.unresolved_thread_count = thread_counts.get(pr.number, 0)

        return prs

    def fetch_all_team_prs(self) -> list[PRData]:
        """Fetch all open PRs across all repos (not filtered by author)."""
        all_prs: list[PRData] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(self.fetch_team_prs, repo): repo for repo in self.config.repos}
            for future in as_completed(futures):
                try:
                    all_prs.extend(future.result())
                except Exception as e:
                    log.error("Error fetching team PRs for %s: %s", futures[future], e)
        return all_prs

    def has_user_review(self, repo: str, number: int, current_user: str, head_sha: str) -> bool:
        """Check if the current user has left a review comment after the latest commit."""
        query = (
            f'query {{ repository(owner: "{self.config.org}", name: "{repo}") {{'
            f'  pullRequest(number: {number}) {{'
            f'    commits(last: 1) {{ nodes {{ commit {{ committedDate }} }} }}'
            f'    comments(last: 50) {{ nodes {{ author {{ login }} createdAt }} }}'
            f'    reviews(last: 50) {{ nodes {{ author {{ login }} createdAt }} }}'
            f'  }}'
            f'}}}}'
        )
        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={query}"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                log.error("GraphQL user review check failed for %s#%d: %s", repo, number, result.stderr.strip())
                return False
            data = json.loads(result.stdout)
            pr_data = data.get("data", {}).get("repository", {}).get("pullRequest", {})
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            log.error("Failed to check user review for %s#%d: %s", repo, number, e)
            return False

        commits = pr_data.get("commits", {}).get("nodes", [])
        if not commits:
            return False
        latest_commit_time = commits[0].get("commit", {}).get("committedDate", "")

        # Check PR comments and reviews by the current user after latest commit
        for source in ("comments", "reviews"):
            for node in pr_data.get(source, {}).get("nodes", []):
                author = node.get("author", {}).get("login", "")
                created = node.get("createdAt", "")
                if author == current_user and created > latest_commit_time:
                    return True
        return False

    def list_repos(self, org: str) -> list[str]:
        try:
            result = subprocess.run(
                ["gh", "repo", "list", org, "--json", "name", "--limit", "100"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                log.error("gh repo list failed: %s", result.stderr.strip())
                return []
            repos = json.loads(result.stdout)
            return sorted(r["name"] for r in repos)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            log.error("Failed to list repos: %s", e)
            return []
