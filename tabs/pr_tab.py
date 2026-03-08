"""Pull Requests tab — table with status, context menu, auto-refresh."""

from __future__ import annotations

import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import ttk
from typing import Callable, Optional

from config import Config
from models import PRData, PRStatus


class PRTab(ttk.Frame):
    def __init__(self, parent: ttk.Notebook, on_update_branch: Callable,
                 on_launch_fix: Callable, on_send_for_review: Callable,
                 on_merge: Callable | None = None,
                 on_mark_ready: Callable | None = None,
                 on_watch: Callable | None = None):
        super().__init__(parent)
        self._on_update_branch = on_update_branch
        self._on_launch_fix = on_launch_fix
        self._on_send_for_review = on_send_for_review
        self._on_merge = on_merge
        self._on_mark_ready = on_mark_ready
        self._on_watch = on_watch
        self._prs: list[PRData] = []
        self._last_poll: Optional[float] = None
        self._watched_keys: set[str] = set()

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

    # Action column icons
    _FIX_ICON = "\U0001f527 Fix"       # 🔧 Fix
    _REVIEW_ICON = "\U0001f4ac Review"  # 💬 Review
    _MERGE_ICON = "\U0001f680 Merge"   # 🚀 Merge
    _DRAFT_ICON = "\U0001F4DD"         # 📝

    def _build_toolbar(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=5, pady=5)

        self._refresh_btn = ttk.Button(toolbar, text="Refresh", command=self._on_refresh)
        self._refresh_btn.pack(side="left")

        self._fix_all_btn = ttk.Button(toolbar, text="Fix All", command=self._fix_all)
        self._fix_all_btn.pack(side="left", padx=(5, 0))

        self._review_btn = ttk.Button(toolbar, text="Send All for Review", command=self._send_all_for_review)
        self._review_btn.pack(side="left", padx=(5, 0))

        self._poll_label = ttk.Label(toolbar, text="Last poll: never")
        self._poll_label.pack(side="left", padx=10)

        self._monitor_label = ttk.Label(toolbar, text="Monitor: starting...", foreground="gray")
        self._monitor_label.pack(side="right")

    def _build_table(self):
        columns = ("action", "repo", "number", "title", "branch", "draft", "status", "url")
        self._tree = ttk.Treeview(self, columns=columns, show="headings", selectmode="browse")

        self._tree.heading("action", text="")
        self._tree.heading("repo", text="Repo")
        self._tree.heading("number", text="PR #")
        self._tree.heading("title", text="Title")
        self._tree.heading("branch", text="Branch")
        self._tree.heading("draft", text="")
        self._tree.heading("status", text="Status")
        self._tree.heading("url", text="URL")

        self._tree.column("action", width=90, minwidth=70, anchor="center")
        self._tree.column("repo", width=130, minwidth=80)
        self._tree.column("number", width=60, minwidth=50)
        self._tree.column("title", width=300, minwidth=150)
        self._tree.column("branch", width=200, minwidth=100)
        self._tree.column("draft", width=30, minwidth=30, anchor="center", stretch=False)
        self._tree.column("status", width=150, minwidth=100)
        self._tree.column("url", width=0, stretch=False)  # Hidden, used for data

        # Scrollbar
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

        # Tooltip label (hidden, shown on hover)
        tip_bg, tip_fg = self._tooltip_colors()
        self._tooltip = tk.Label(
            self.winfo_toplevel(), text="", background=tip_bg, foreground=tip_fg,
            relief="solid", borderwidth=1, font=("system", 11),
        )
        self._default_cursor = self._tree["cursor"] or ""

        # Events
        self._tree.bind("<ButtonRelease-1>", self._on_click)
        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<Button-2>", self._on_right_click)  # macOS right-click
        self._tree.bind("<Control-Button-1>", self._on_right_click)
        self._tree.bind("<Motion>", self._on_motion)
        self._tree.bind("<Leave>", self._on_leave)

    def _build_context_menu(self):
        self._ctx_menu = tk.Menu(self, tearoff=0)
        self._ctx_menu.add_command(label="Open in Browser", command=self._ctx_open_browser)
        self._ctx_menu.add_command(label="Update Branch", command=self._ctx_update_branch)
        self._ctx_menu.add_command(label="Launch Claude Fix", command=self._ctx_launch_fix)
        self._ctx_menu.add_command(label="Watch (Auto-Fix)", command=self._ctx_toggle_watch)
        self._ctx_watch_index = self._ctx_menu.index("end")
        self._ctx_menu.add_command(label="Mark Ready (Remove Draft)", command=self._ctx_mark_ready)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Send for Review", command=self._ctx_send_for_review)
        self._ctx_menu.add_command(label="Merge", command=self._ctx_merge)

    @staticmethod
    def _tooltip_colors() -> tuple[str, str]:
        """Return (background, foreground) for tooltips, respecting dark mode."""
        try:
            import subprocess
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0 and "Dark" in result.stdout:
                return "#3a3a3c", "#e5e5e7"
        except Exception:
            pass
        return "#ffffe0", "#000000"

    def _on_refresh(self):
        """Called by refresh button — triggers the external refresh callback."""
        # The main app will connect this
        pass

    def set_refresh_callback(self, callback: Callable):
        self._refresh_btn.configure(command=callback)

    def set_watched_keys(self, keys: set[str]):
        self._watched_keys = keys
        if self._prs:
            self.update_prs(self._prs)

    def update_prs(self, prs: list[PRData]):
        if self._loading_visible:
            self._loading_frame.destroy()
            self._loading_visible = False

        # Preserve selection
        selected_key = None
        sel = self._tree.selection()
        if sel:
            vals = self._tree.item(sel[0], "values")
            selected_key = (vals[1], vals[2])  # (repo, #number)

        self._prs = prs
        self._tree.delete(*self._tree.get_children())

        for pr in sorted(prs, key=lambda p: (p.repo, p.number)):
            tag = pr.status.value
            key = f"{pr.repo}#{pr.number}"
            if pr.status == PRStatus.APPROVED:
                action_text = self._MERGE_ICON
            elif pr.is_ready_for_review:
                action_text = self._REVIEW_ICON
            elif pr.issues:
                action_text = self._FIX_ICON
            else:
                action_text = ""  # Pending — no action yet
            if key in self._watched_keys:
                action_text = "\U0001f441 " + action_text  # 👁 prefix
            draft_icon = self._DRAFT_ICON if pr.is_draft else ""
            self._tree.insert("", "end", values=(
                action_text,
                pr.repo,
                f"#{pr.number}",
                pr.title,
                pr.branch,
                draft_icon,
                pr.status_display,
                pr.url,
            ), tags=(tag,))

        # Restore selection
        if selected_key:
            for item in self._tree.get_children():
                vals = self._tree.item(item, "values")
                if (vals[1], vals[2]) == selected_key:
                    self._tree.selection_set(item)
                    self._tree.focus(item)
                    break

    def update_poll_time(self, timestamp: float):
        self._last_poll = timestamp
        time_str = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
        self._poll_label.configure(text=f"Last poll: {time_str}")
        self._monitor_label.configure(text="Monitor: active", foreground="#2da44e")

    def _get_selected_pr(self) -> Optional[PRData]:
        sel = self._tree.selection()
        if not sel:
            return None
        values = self._tree.item(sel[0], "values")
        repo = values[1]       # action is [0], repo is [1]
        number = int(values[2].lstrip("#"))  # number is [2]
        for pr in self._prs:
            if pr.repo == repo and pr.number == number:
                return pr
        return None

    def _on_click(self, event):
        """Single-click handler — triggers action column buttons."""
        col = self._tree.identify_column(event.x)
        if col != "#1":  # action column
            return
        item = self._tree.identify_row(event.y)
        if not item:
            return
        self._tree.selection_set(item)
        pr = self._get_selected_pr()
        if not pr:
            return
        if pr.status == PRStatus.APPROVED and self._on_merge:
            self._on_merge(pr)
        elif pr.is_ready_for_review:
            self._on_send_for_review(pr)
        elif pr.issues:
            self._on_launch_fix(pr)

    def _on_double_click(self, event):
        col = self._tree.identify_column(event.x)
        if col == "#1":  # action column — already handled by single click
            return
        pr = self._get_selected_pr()
        if pr:
            webbrowser.open(pr.url)

    def _on_motion(self, event):
        """Show pointer cursor and tooltip when hovering over action/status columns."""
        col = self._tree.identify_column(event.x)
        item = self._tree.identify_row(event.y)
        tip = ""

        if item:
            values = self._tree.item(item, "values")
            if col == "#1" and values:
                action_text = values[0]
                if action_text:
                    self._tree.configure(cursor="hand2")
                    if self._MERGE_ICON in action_text:
                        tip = "Merge this pull request"
                    elif self._FIX_ICON in action_text:
                        tip = "Send to Claude to fix issues"
                    elif self._REVIEW_ICON in action_text:
                        tip = "Send Slack message for review"
            elif col == "#5" and values:
                self._tree.configure(cursor="hand2")
            elif col == "#6" and values:
                if values[5] == self._DRAFT_ICON:
                    tip = "Draft"

        if tip:
            self._tooltip.configure(text=tip)
            x = event.x_root + 12
            y = event.y_root + 12
            self._tooltip.place(x=0, y=0)
            self._tooltip.lift()
            self._tooltip.place_forget()
            self._tooltip.place(in_=self.winfo_toplevel(),
                                x=x - self.winfo_toplevel().winfo_rootx(),
                                y=y - self.winfo_toplevel().winfo_rooty())
            return

        if col not in ("#1", "#5"):
            self._tree.configure(cursor=self._default_cursor)
        self._tooltip.place_forget()

    def _on_leave(self, event):
        """Hide tooltip and restore cursor when leaving the tree."""
        self._tree.configure(cursor=self._default_cursor)
        self._tooltip.place_forget()

    def _on_right_click(self, event):
        item = self._tree.identify_row(event.y)
        if item:
            self._tree.selection_set(item)
            pr = self._get_selected_pr()
            if pr:
                key = f"{pr.repo}#{pr.number}"
                is_watched = key in self._watched_keys
                label = "Unwatch" if is_watched else "Watch (Auto-Fix)"
                self._ctx_menu.entryconfigure(self._ctx_watch_index, label=label)
            self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _ctx_open_browser(self):
        pr = self._get_selected_pr()
        if pr:
            webbrowser.open(pr.url)

    def _ctx_update_branch(self):
        pr = self._get_selected_pr()
        if pr:
            self._on_update_branch(pr.repo, pr.number)

    def _ctx_launch_fix(self):
        pr = self._get_selected_pr()
        if pr:
            self._on_launch_fix(pr)

    def _ctx_toggle_watch(self):
        pr = self._get_selected_pr()
        if pr and self._on_watch:
            key = f"{pr.repo}#{pr.number}"
            is_watched = key in self._watched_keys
            self._on_watch(pr, not is_watched)

    def _fix_all(self):
        """Launch Claude fix for all PRs that have issues."""
        fixable = [pr for pr in self._prs if pr.issues and not pr.is_draft]
        if not fixable:
            from tkinter import messagebox
            messagebox.showinfo("Nothing to Fix", "No PRs currently have issues to fix.", parent=self)
            return
        for pr in fixable:
            self._on_launch_fix(pr, batch=True)

    def _send_all_for_review(self):
        """Send all passing PRs for review."""
        ready = [pr for pr in self._prs if pr.is_ready_for_review]
        if not ready:
            from tkinter import messagebox
            messagebox.showinfo("No PRs Ready", "No PRs are currently ready for review.", parent=self)
            return
        for pr in ready:
            self._on_send_for_review(pr)

    def _ctx_send_for_review(self):
        pr = self._get_selected_pr()
        if not pr:
            return
        if not pr.is_ready_for_review:
            from tkinter import messagebox
            messagebox.showwarning(
                "Not Ready",
                f"PR #{pr.number} is not ready for review ({pr.status_display}).\n"
                "Fix issues before sending for review.",
                parent=self,
            )
            return
        self._on_send_for_review(pr)

    def _ctx_mark_ready(self):
        pr = self._get_selected_pr()
        if not pr:
            return
        if not pr.is_draft:
            from tkinter import messagebox
            messagebox.showinfo("Not a Draft", f"PR #{pr.number} is not a draft.", parent=self)
            return
        if self._on_mark_ready:
            self._on_mark_ready(pr)

    def _ctx_merge(self):
        pr = self._get_selected_pr()
        if not pr:
            return
        if pr.status != PRStatus.APPROVED:
            from tkinter import messagebox
            messagebox.showwarning(
                "Not Ready",
                f"PR #{pr.number} is not approved ({pr.status_display}).\n"
                "PR must be approved before merging.",
                parent=self,
            )
            return
        if self._on_merge:
            self._on_merge(pr)
