"""PR Review tab — review team PRs using Claude's /address-review skill."""

from __future__ import annotations

import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import ttk, messagebox
from typing import Callable, Optional

from models import PRData


# Review status constants
STATUS_PENDING = "pending"
STATUS_REVIEWING = "reviewing"
STATUS_REVIEWED = "reviewed"
STATUS_REVIEWED_BY_ME = "reviewed_by_me"
STATUS_REVIEWED_BY_CLAUDE = "reviewed_by_claude"

_STATUS_DISPLAY = {
    STATUS_PENDING: "Awaiting Review",
    STATUS_REVIEWING: "\u23f3 Reviewing...",
    STATUS_REVIEWED: "\u2705 Reviewed",
    STATUS_REVIEWED_BY_ME: "\u2705 Reviewed by Me",
    STATUS_REVIEWED_BY_CLAUDE: "\u2705 Reviewed by Claude",
}


class PRReviewTab(ttk.Frame):
    def __init__(
        self,
        parent: ttk.Notebook,
        on_run_review: Callable[[PRData], None],
        on_run_review_all: Callable[[list[PRData]], None],
    ):
        super().__init__(parent)
        self._on_run_review = on_run_review
        self._on_run_review_all = on_run_review_all
        self._prs: list[PRData] = []
        self._review_statuses: dict[str, str] = {}
        self._last_poll: Optional[float] = None

        self._build_toolbar()
        self._build_table()
        self._build_context_menu()
        self._build_loading_overlay()

    def _build_loading_overlay(self):
        self._loading_visible = True
        self._loading_frame = ttk.Frame(self)
        self._loading_label = ttk.Label(
            self._loading_frame, text="\u23f3 Loading pull requests...",
            font=("system", 14),
        )
        self._loading_label.place(relx=0.5, rely=0.5, anchor="center")
        self._loading_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._loading_frame.lift()

    def _build_toolbar(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=5, pady=5)

        self._refresh_btn = ttk.Button(toolbar, text="Refresh")
        self._refresh_btn.pack(side="left")

        self._review_selected_btn = ttk.Button(
            toolbar, text="Review Selected", command=self._review_selected,
        )
        self._review_selected_btn.pack(side="left", padx=(5, 0))

        self._review_all_btn = ttk.Button(
            toolbar, text="Review All", command=self._review_all,
        )
        self._review_all_btn.pack(side="left", padx=(5, 0))

        self._poll_label = ttk.Label(toolbar, text="Last poll: never")
        self._poll_label.pack(side="left", padx=10)

        self._count_label = ttk.Label(toolbar, text="0 PRs for review")
        self._count_label.pack(side="right")

        self._hide_drafts_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="Hide Drafts", variable=self._hide_drafts_var,
                        command=self._refresh_table).pack(side="right", padx=(0, 10))

    _REVIEW_ICON = "\U0001F50D Review"  # 🔍 Review
    _DRAFT_ICON = "\U0001F4DD"         # 📝

    def _build_table(self):
        columns = ("action", "repo", "number", "title", "branch", "draft", "author", "status", "review_status", "url")
        self._tree = ttk.Treeview(self, columns=columns, show="headings", selectmode="extended")

        self._tree.heading("action", text="")
        self._tree.heading("repo", text="Repo")
        self._tree.heading("number", text="PR #")
        self._tree.heading("title", text="Title")
        self._tree.heading("branch", text="Branch")
        self._tree.heading("draft", text="")
        self._tree.heading("author", text="Author")
        self._tree.heading("status", text="Status")
        self._tree.heading("review_status", text="Review")
        self._tree.heading("url", text="URL")

        self._tree.column("action", width=90, minwidth=70, anchor="center")
        self._tree.column("repo", width=130, minwidth=80)
        self._tree.column("number", width=60, minwidth=50)
        self._tree.column("title", width=250, minwidth=150)
        self._tree.column("branch", width=170, minwidth=100)
        self._tree.column("draft", width=30, minwidth=30, anchor="center", stretch=False)
        self._tree.column("author", width=100, minwidth=80)
        self._tree.column("status", width=150, minwidth=100)
        self._tree.column("review_status", width=130, minwidth=80)
        self._tree.column("url", width=0, stretch=False)

        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)

        self._tree.pack(side="left", fill="both", expand=True, padx=(5, 0), pady=5)
        scrollbar.pack(side="right", fill="y", pady=5, padx=(0, 5))

        # Tags for color-coding
        self._tree.tag_configure("passing", foreground="#2da44e")
        self._tree.tag_configure("approved", foreground="#1a7f37")
        self._tree.tag_configure("behind", foreground="#d29922")
        self._tree.tag_configure("conflicts", foreground="#cf222e")
        self._tree.tag_configure("ci_failing", foreground="#cf222e")
        self._tree.tag_configure("changes_requested", foreground="#8250df")
        self._tree.tag_configure("pending", foreground="#636c76")
        self._tree.tag_configure(STATUS_REVIEWING, foreground="#d29922")
        self._tree.tag_configure(STATUS_REVIEWED, foreground="#2da44e")
        self._tree.tag_configure(STATUS_REVIEWED_BY_ME, foreground="#2da44e")
        self._tree.tag_configure(STATUS_REVIEWED_BY_CLAUDE, foreground="#2da44e")

        self._default_cursor = self._tree["cursor"] or ""

        # Events
        self._tree.bind("<ButtonRelease-1>", self._on_click)
        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<Button-2>", self._on_right_click)
        self._tree.bind("<Control-Button-1>", self._on_right_click)
        self._tree.bind("<Motion>", self._on_motion)
        self._tree.bind("<Leave>", self._on_leave)

    def _build_context_menu(self):
        self._ctx_menu = tk.Menu(self, tearoff=0)
        self._ctx_menu.add_command(label="Run Claude Review", command=self._ctx_run_review)
        self._ctx_menu.add_command(label="Open in Browser", command=self._ctx_open_browser)

    def set_refresh_callback(self, callback: Callable):
        self._refresh_btn.configure(command=callback)

    # ── Public API ────────────────────────────────────────────────────────

    def update_prs(self, prs: list[PRData], review_statuses: dict[str, str]):
        if self._loading_visible:
            self._loading_frame.destroy()
            self._loading_visible = False

        self._prs = prs
        self._review_statuses = review_statuses
        self._refresh_table()

    def _refresh_table(self):
        # Preserve selection
        selected_keys = set()
        for sel in self._tree.selection():
            vals = self._tree.item(sel, "values")
            selected_keys.add((vals[1], vals[2]))  # (repo, #number)

        self._tree.delete(*self._tree.get_children())

        hide_drafts = self._hide_drafts_var.get()
        visible_prs = [pr for pr in self._prs if not (hide_drafts and pr.is_draft)]

        for pr in sorted(visible_prs, key=lambda p: (p.repo, p.number)):
            key = f"{pr.repo}#{pr.number}"
            status = self._review_statuses.get(key, STATUS_PENDING)
            action = self._REVIEW_ICON if status != STATUS_REVIEWING else ""
            draft_icon = self._DRAFT_ICON if pr.is_draft else ""
            pr_tag = "passing" if pr.is_ready_for_review else pr.status.value
            self._tree.insert("", "end", values=(
                action,
                pr.repo,
                f"#{pr.number}",
                pr.title,
                pr.branch,
                draft_icon,
                pr.author,
                pr.status_display,
                _STATUS_DISPLAY.get(status, status),
                pr.url,
            ), tags=(pr_tag,))

        reviewable = sum(
            1 for pr in visible_prs
            if self._review_statuses.get(f"{pr.repo}#{pr.number}", STATUS_PENDING) == STATUS_PENDING
        )
        total = len(self._prs)
        shown = len(visible_prs)
        hidden = total - shown
        count_text = f"{shown} PRs ({reviewable} awaiting review)"
        if hidden:
            count_text += f" — {hidden} draft hidden"
        self._count_label.configure(text=count_text)

        # Restore selection and focus
        if selected_keys:
            to_select = []
            for item in self._tree.get_children():
                vals = self._tree.item(item, "values")
                if (vals[1], vals[2]) in selected_keys:
                    to_select.append(item)
            if to_select:
                self._tree.selection_set(*to_select)
                self._tree.focus(to_select[0])

    def update_poll_time(self, timestamp: float):
        self._last_poll = timestamp
        time_str = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
        self._poll_label.configure(text=f"Last poll: {time_str}")

    def update_review_status(self, repo: str, number: int, status: str):
        key = f"{repo}#{number}"
        self._review_statuses[key] = status
        for item in self._tree.get_children():
            values = self._tree.item(item, "values")
            if values[1] == repo and values[2] == f"#{number}":
                new_values = list(values)
                new_values[0] = self._REVIEW_ICON if status != STATUS_REVIEWING else ""
                new_values[8] = _STATUS_DISPLAY.get(status, status)
                self._tree.item(item, values=tuple(new_values))
                break
        # Update count
        reviewable = sum(
            1 for pr in self._prs
            if self._review_statuses.get(f"{pr.repo}#{pr.number}", STATUS_PENDING) == STATUS_PENDING
        )
        self._count_label.configure(text=f"{len(self._prs)} PRs ({reviewable} awaiting review)")

    # ── Private ───────────────────────────────────────────────────────────

    def _get_selected_prs(self) -> list[PRData]:
        result = []
        for item in self._tree.selection():
            values = self._tree.item(item, "values")
            repo = values[1]       # action is [0], repo is [1]
            number = int(values[2].lstrip("#"))  # number is [2]
            for pr in self._prs:
                if pr.repo == repo and pr.number == number:
                    result.append(pr)
                    break
        return result

    def _get_reviewable_prs(self) -> list[PRData]:
        return [
            pr for pr in self._prs
            if pr.all_checks_passed
            and not pr.is_draft
            and self._review_statuses.get(
                f"{pr.repo}#{pr.number}", STATUS_PENDING
            ) not in (STATUS_REVIEWING, STATUS_REVIEWED, STATUS_REVIEWED_BY_ME, STATUS_REVIEWED_BY_CLAUDE)
        ]

    def _review_selected(self):
        prs = self._get_selected_prs()
        # Filter out any currently being reviewed
        reviewable = [
            pr for pr in prs
            if self._review_statuses.get(
                f"{pr.repo}#{pr.number}", STATUS_PENDING
            ) != STATUS_REVIEWING
        ]
        if not reviewable:
            messagebox.showinfo("No PRs to Review", "No reviewable PRs selected.", parent=self)
            return
        self._on_run_review_all(reviewable)

    def _review_all(self):
        reviewable = self._get_reviewable_prs()
        if not reviewable:
            messagebox.showinfo("No PRs to Review", "All PRs have already been reviewed.", parent=self)
            return
        self._on_run_review_all(reviewable)

    def _on_click(self, event):
        col = self._tree.identify_column(event.x)
        if col != "#1":  # action column
            return
        item = self._tree.identify_row(event.y)
        if not item:
            return
        self._tree.selection_set(item)
        prs = self._get_selected_prs()
        if prs:
            self._on_run_review(prs[0])

    def _on_double_click(self, event):
        col = self._tree.identify_column(event.x)
        if col == "#1":
            return
        prs = self._get_selected_prs()
        if prs:
            webbrowser.open(prs[0].url)

    def _on_motion(self, event):
        col = self._tree.identify_column(event.x)
        item = self._tree.identify_row(event.y)
        if item:
            if col == "#1":
                values = self._tree.item(item, "values")
                if values and values[0]:
                    self._tree.configure(cursor="hand2")
                    return
            elif col == "#5":
                self._tree.configure(cursor="hand2")
                return
        if col not in ("#1", "#5"):
            self._tree.configure(cursor=self._default_cursor)

    def _on_leave(self, _event):
        self._tree.configure(cursor=self._default_cursor)

    def _on_right_click(self, event):
        item = self._tree.identify_row(event.y)
        if item:
            self._tree.selection_set(item)
            prs = self._get_selected_prs()
            if prs:
                key = f"{prs[0].repo}#{prs[0].number}"
                status = self._review_statuses.get(key, STATUS_PENDING)
                state = "disabled" if status == STATUS_REVIEWING else "normal"
                self._ctx_menu.entryconfigure(0, state=state)
            self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _ctx_run_review(self):
        prs = self._get_selected_prs()
        if prs:
            self._on_run_review(prs[0])

    def _ctx_open_browser(self):
        prs = self._get_selected_prs()
        if prs:
            webbrowser.open(prs[0].url)
