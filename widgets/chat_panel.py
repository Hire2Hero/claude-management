"""Embedded chat panel for Claude CLI interaction."""

from __future__ import annotations

import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk
from typing import Callable, Optional

MAX_LINES = 10_000


class ChatPanel(ttk.Frame):
    """Conversation panel: status bar, scrollable text area, input entry."""

    def __init__(
        self,
        parent: tk.Widget,
        on_send: Callable[[str], None],
        on_stop: Callable[[], None],
        on_close: Callable[[], None] = lambda: None,
        on_toggle_summary: Callable[[], None] = lambda: None,
        on_restart: Callable[[], None] = lambda: None,
    ):
        super().__init__(parent)
        self._on_send = on_send
        self._on_stop = on_stop
        self._on_close = on_close
        self._on_toggle_summary = on_toggle_summary
        self._on_restart = on_restart
        self._build_ui()

    def _build_ui(self):
        # ── Status bar ────────────────────────────────────────────────────
        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", padx=5, pady=(5, 0))

        # Buttons packed right-to-left first so they always have space
        btn_kw = dict(
            borderwidth=1, relief="raised", cursor="hand2",
            padx=2, pady=0, font=("TkDefaultFont", 13),
        )
        self._close_btn = tk.Button(
            status_frame, text="\u2715", command=self._on_close, **btn_kw,
        )
        self._close_btn.pack(side="right")
        self._add_tooltip(self._close_btn, "Close panel")

        self._summary_btn = tk.Button(
            status_frame, text="\U0001F4CB", command=self._on_toggle_summary, **btn_kw,
        )
        self._summary_btn.pack(side="right", padx=(0, 3))
        self._add_tooltip(self._summary_btn, "Toggle summary")

        # PR link — hidden by default, shown when session has a PR URL
        self._pr_url: Optional[str] = None
        self._pr_icon = self._load_pr_icon(btn_kw)
        if self._pr_icon:
            self._pr_btn = tk.Button(
                status_frame, image=self._pr_icon, command=self._open_pr, **btn_kw,
            )
        else:
            self._pr_btn = tk.Button(
                status_frame, text="PR", command=self._open_pr, **btn_kw,
            )

        # Stop — hidden by default, shown for running sessions
        self._stop_btn = tk.Button(
            status_frame, text="\U0001F6D1", command=self._on_stop, **btn_kw,
        )
        self._add_tooltip(self._stop_btn, "Stop session")

        # Restart — hidden by default, shown for stopped sessions
        self._restart_btn = tk.Button(
            status_frame, text="\U0001F504", command=self._on_restart, **btn_kw,
        )
        self._add_tooltip(self._restart_btn, "Restart with fresh plugins")

        # Status label last so it fills remaining space and truncates
        self._status_label = tk.Label(
            status_frame,
            text="No session",
            foreground="#636c76",
            background=ttk.Style().lookup("TFrame", "background"),
            font=("TkDefaultFont", 0),
            anchor="w",
        )
        self._status_label.pack(side="left", fill="x", expand=True)

        # ── Text display ──────────────────────────────────────────────────
        text_frame = ttk.Frame(self)
        text_frame.pack(fill="both", expand=True, padx=5, pady=5)

        self._text = tk.Text(
            text_frame,
            wrap="word",
            state="disabled",
            font=("Menlo", 11),
            background="#1e1e1e",
            foreground="#d4d4d4",
            insertbackground="#d4d4d4",
            selectbackground="#264f78",
            borderwidth=0,
            highlightthickness=0,
            padx=8,
            pady=8,
        )
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical", command=self._text.yview)
        self._text.configure(yscrollcommand=scrollbar.set)

        self._text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Text tags
        self._text.tag_configure("assistant", foreground="#d4d4d4")
        self._text.tag_configure("user", foreground="#569cd6")
        self._text.tag_configure("tool", foreground="#9cdcfe", font=("Menlo", 11, "bold"))
        self._text.tag_configure("tool_done", foreground="#6a9955")
        self._text.tag_configure("error", foreground="#f44747")
        self._text.tag_configure("system", foreground="#808080", font=("Menlo", 11, "italic"))

        # ── Skill continuation banner (hidden by default) ────────────────
        self._continuation_frame = ttk.Frame(self)
        # Not packed — shown/hidden dynamically

        self._continuation_label = ttk.Label(
            self._continuation_frame, text="", wraplength=500
        )
        self._continuation_label.pack(side="left", fill="x", expand=True, padx=(8, 5))

        self._cont_stop_btn = ttk.Button(
            self._continuation_frame, text="Stop", width=6
        )
        self._cont_stop_btn.pack(side="right", padx=(0, 5))

        self._cont_continue_btn = ttk.Button(
            self._continuation_frame, text="Continue", width=8
        )
        self._cont_continue_btn.pack(side="right", padx=(0, 3))

        # ── Input bar ─────────────────────────────────────────────────────
        self._input_frame = ttk.Frame(self)
        self._input_frame.pack(fill="x", padx=5, pady=(0, 5))

        self._input = ttk.Entry(self._input_frame)
        self._input.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self._input.bind("<Return>", self._handle_return)
        self._input.bind("<Command-v>", self._handle_paste)
        self._input.bind("<Control-v>", self._handle_paste)

        self._send_btn = ttk.Button(
            self._input_frame, text="Send", command=self._handle_send
        )
        self._send_btn.pack(side="right")

        self.set_input_enabled(False)

    # ── Public API ────────────────────────────────────────────────────────

    def set_status(self, text: str, running: bool = False):
        self._status_label.configure(
            text=text,
            foreground="#2da44e" if running else "#636c76",
        )
        if running:
            self._stop_btn.pack(side="right", padx=(0, 5), before=self._summary_btn)
            self._restart_btn.pack_forget()
        else:
            self._stop_btn.pack_forget()
            self._restart_btn.pack(side="right", padx=(0, 5), before=self._summary_btn)

    def set_input_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self._input.configure(state=state)
        self._send_btn.configure(state=state)
        if enabled:
            self._input.focus_set()

    def set_pr_url(self, url: Optional[str]):
        self._pr_url = url
        if url:
            self._pr_btn.pack(side="right", padx=(0, 5), before=self._summary_btn)
        else:
            self._pr_btn.pack_forget()

    def show_skill_continuation(
        self,
        step_info: str,
        next_label: str,
        on_continue: Callable[[], None],
        on_stop: Callable[[], None],
    ):
        self._continuation_label.configure(
            text=f"{step_info}  Next: {next_label}"
        )
        self._cont_continue_btn.configure(command=on_continue)
        self._cont_stop_btn.configure(command=on_stop)
        self._continuation_frame.pack(fill="x", padx=5, pady=(0, 3), before=self._input_frame)

    def hide_skill_continuation(self):
        self._continuation_frame.pack_forget()

    def clear(self):
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")

    def load_history(self, entries: list[tuple[str, str]]):
        """Bulk-load history entries into the text widget efficiently.

        Performs all inserts in a single normal/disabled cycle with no
        per-entry trim or scroll, avoiding the O(n) widget updates that
        make replay painfully slow for long sessions.
        """
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        for tag, text in entries:
            self._text.insert("end", text, tag)
        self._trim()
        self._text.configure(state="disabled")
        self._text.see("end")

    def append_text(self, text: str, tag: str = "assistant"):
        """Append text (typically streaming delta) to the display."""
        self._text.configure(state="normal")
        self._text.insert("end", text, tag)
        self._trim()
        self._text.configure(state="disabled")
        self._text.see("end")

    def append_user_message(self, text: str):
        self._text.configure(state="normal")
        self._text.insert("end", f"\nYou: {text}\n", "user")
        self._trim()
        self._text.configure(state="disabled")
        self._text.see("end")

    def append_tool_start(self, summary: str):
        self._text.configure(state="normal")
        self._text.insert("end", f"\n[{summary}]\n", "tool")
        self._text.configure(state="disabled")
        self._text.see("end")

    def append_tool_done(self):
        """No-op kept for compatibility — tool completion is implicit."""
        pass

    def append_error(self, text: str):
        self._text.configure(state="normal")
        self._text.insert("end", f"\nError: {text}\n", "error")
        self._text.configure(state="disabled")
        self._text.see("end")

    def append_system(self, text: str):
        self._text.configure(state="normal")
        self._text.insert("end", f"{text}\n", "system")
        self._text.configure(state="disabled")
        self._text.see("end")

    # ── Private ───────────────────────────────────────────────────────────

    def _load_pr_icon(self, btn_kw: dict) -> Optional[tk.PhotoImage]:
        """Load the PR icon PNG and resize to match emoji button height."""
        icon_path = Path(__file__).resolve().parent.parent / "assets" / "pr_icon.png"
        if not icon_path.exists():
            return None
        try:
            from PIL import Image as PILImage, ImageTk
            # Measure the emoji button height to match
            self._close_btn.update_idletasks()
            border = int(btn_kw.get("borderwidth", 1))
            target = self._close_btn.winfo_reqheight() - 2 * border - 4
            if target < 8:
                target = 20
            pil_img = PILImage.open(icon_path)
            # Scale preserving aspect ratio
            w, h = pil_img.size
            new_w = int(w * target / h)
            pil_img = pil_img.resize((new_w, target), PILImage.LANCZOS)
            photo = ImageTk.PhotoImage(pil_img)
            # Keep a reference to prevent garbage collection
            self._pr_icon_pil = photo
            return photo
        except ImportError:
            # Fallback to raw PhotoImage if PIL not available
            return tk.PhotoImage(file=str(icon_path))

    def _open_pr(self):
        if self._pr_url:
            webbrowser.open(self._pr_url)

    def _handle_paste(self, _event):
        try:
            clipboard = self._input.clipboard_get()
            self._input.insert("insert", clipboard)
        except tk.TclError:
            pass
        return "break"

    def _handle_return(self, _event):
        self._handle_send()

    def _handle_send(self):
        text = self._input.get().strip()
        self._input.delete(0, "end")
        self._on_send(text or "continue")

    def _add_tooltip(self, widget: tk.Widget, text: str):
        """Attach a hover tooltip to a widget."""
        tip = tk.Toplevel(widget)
        tip.withdraw()
        tip.overrideredirect(True)
        label = tk.Label(
            tip, text=text, background="#ffffe0", foreground="#000",
            relief="solid", borderwidth=1, font=("TkDefaultFont", 11),
            padx=4, pady=2,
        )
        label.pack()

        def show(event):
            tip.geometry(f"+{event.x_root + 10}+{event.y_root + 10}")
            tip.deiconify()

        def hide(_event):
            tip.withdraw()

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    def _trim(self):
        """Trim text widget to MAX_LINES."""
        line_count = int(self._text.index("end-1c").split(".")[0])
        if line_count > MAX_LINES:
            overshoot = line_count - MAX_LINES
            self._text.delete("1.0", f"{overshoot}.0")
