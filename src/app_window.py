"""Main application window for flow-clone (v2 U4, plan §U4).

The Wispr-Flow-like face of the tool: a dark customtkinter window with a
sidebar (Transcripts / Dictionary), a status footer, and withdraw-not-
destroy lifecycle over the shared tk root. Opened from the tray ("Open
Flow Clone", also the icon's default action) via the ("open_app",) ui_q
message; the title-bar X or Esc withdraws it so reopening is instant.

DPI note (plan risk #1): `import customtkinter` here sets process-wide DPI
awareness — main.py imports this module at its top, BEFORE tk.Tk() is
created, so every window in the process lives under one DPI regime.

Single-writer invariant (plan risk #5): this window NEVER touches the
Dictionary. It has no reference to it at all — dictionary rows render from
worker-posted deepcopy snapshots delivered through refresh_dictionary(),
and every mutation is a job posted via the `post_job` closure
(job_q.put), executed by the single pipeline worker:

    ("teach", original, edited)          Save & teach on a transcript
    ("dict_toggle", canonical, enabled)  per-entry enable switch
    ("dict_delete", canonical)           per-entry delete button

After any of those the worker posts ("dict_changed", snapshot) back on
ui_q and the poll loop calls refresh_dictionary(snapshot) here.

History is read directly (History has its own lock — main-thread reads
are safe) but never written: History is an immutable log; the Edit box
feeds the LEARNER only, it never rewrites a transcript.

Threading: ALL methods run on the tkinter main thread only — show(),
refresh_dictionary(), refresh_if_visible() and set_paused() are called
from main.py's ui_q poll loop / pause path, and everything else is a tk
callback.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

import customtkinter as ctk

from src.history import History
from src.injector import set_clipboard_text

log = logging.getLogger(__name__)

ctk.set_appearance_mode("dark")

#: App icon (title bar + taskbar). CTkToplevel sets its OWN icon during init
#: via a scheduled after(), so we assert ours after that settles (see __init__).
ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "flowclone.ico"

#: Rows actually rendered in the transcripts list. CTk row widgets are
#: heavyweight (each is a canvas); 50 keeps show() snappy. History still
#: caps the on-disk log at 200 — the footer note points there.
_MAX_LIST_ROWS = 50
_PREVIEW_CHARS = 96

# --- palette (fixed dark theme — the pill/tray are dark too) ------------ #
_BG = "#16181D"
_SIDEBAR = "#101216"
_CARD = "#22252C"
_CARD_SELECTED = "#333A46"
_TEXT = "#E8EAED"
_MUTED = "#8B919A"
_ACCENT = "#6D7FE0"
_DANGER_HOVER = "#8A3B3B"

#: outcome -> (badge background, badge text color)
_OUTCOME_COLORS = {
    "pasted": ("#1E3527", "#7BD88F"),
    "window_changed": ("#3A331C", "#E5C07B"),
    "no_target": ("#3A2323", "#E06C75"),
    "paste_failed": ("#3A2323", "#E06C75"),
    "discarded": ("#2A2E36", "#9AA0A6"),
}
_OUTCOME_FALLBACK = ("#2A2E36", "#9AA0A6")


def _preview(text: str) -> str:
    """First ~96 chars, newlines flattened, ellipsis when truncated."""
    flat = " ".join(text.split())
    if len(flat) <= _PREVIEW_CHARS:
        return flat
    return flat[:_PREVIEW_CHARS].rstrip() + "…"


def _fmt_when(ts: str) -> str:
    """'2026-07-04T14:31:07-04:00' -> 'today 14:31 · 2h ago' (best-effort;
    older entries get the absolute 'MM-DD HH:MM' only)."""
    try:
        dt = datetime.fromisoformat(ts)
        now = datetime.now(dt.tzinfo)
        delta = (now - dt).total_seconds()
        absolute = f"{dt:%m-%d %H:%M}"
        if delta < 0 or delta >= 86400:
            return absolute
        if delta < 60:
            rel = "just now"
        elif delta < 3600:
            rel = f"{int(delta // 60)}m ago"
        else:
            rel = f"{int(delta // 3600)}h ago"
        return f"{dt:%H:%M} · {rel}"
    except Exception:  # noqa: BLE001 — cosmetic only, never fatal
        return ts[:16]


class AppWindow(ctk.CTkToplevel):
    """Reusable dark app window: transcripts + dictionary + status footer.

    Constructor: (root, history, post_job, config) — `post_job` is a
    job_q.put closure; there is deliberately NO dictionary parameter.
    """

    def __init__(self, root, history: History,
                 post_job: Callable[[tuple], None], config: dict) -> None:
        super().__init__(root, fg_color=_BG)
        self._history = history
        self._post_job = post_job
        self._config = config

        self._entries: list[dict] = []      # rendered transcript rows
        self._selected: int | None = None
        self._t_rows: list[ctk.CTkFrame] = []
        self._dict_snapshot: list[dict] = []  # worker-posted deepcopies only
        self._d_switches: dict[str, ctk.CTkSwitch] = {}
        self._paused = False
        self._wants_visible = False

        self.withdraw()
        self.title("Murmur")
        self.geometry("900x600")
        self.minsize(720, 480)
        self._apply_icon()
        # X hides (reusable window), never destroys.
        self.protocol("WM_DELETE_WINDOW", self.hide)
        self.bind("<Escape>", lambda _e: self.hide())
        # CTkToplevel schedules internal after() work at construction (icon,
        # titlebar color) that can transiently re-map the window — re-assert
        # withdrawn once that settles, unless show() ran in the meantime.
        # The icon is re-asserted in the SAME callback because ctk's scheduled
        # work overwrites it with the default feather otherwise.
        self.after(350, self._settle)

        self._font = ctk.CTkFont(family="Segoe UI", size=13)
        self._font_small = ctk.CTkFont(family="Segoe UI", size=11)
        self._font_bold = ctk.CTkFont(family="Segoe UI", size=13, weight="bold")
        self._font_title = ctk.CTkFont(family="Segoe UI", size=17, weight="bold")

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_transcripts_view()
        self._build_dictionary_view()
        self._build_footer()
        self._show_view("transcripts")

    # ------------------------------------------------------------------ #
    # Public API — main thread only                                       #
    # ------------------------------------------------------------------ #

    def show(self) -> None:
        """Full refresh, then bring the window up. Taking focus here is
        fine — the user explicitly asked for the window via the tray."""
        self._wants_visible = True
        self._refresh_transcripts()
        self._rebuild_dictionary()
        self._refresh_footer()
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide(self) -> None:
        self._wants_visible = False
        self.withdraw()

    def refresh_dictionary(self, snapshot: list[dict]) -> None:
        """Adopt the worker's latest deepcopy snapshot. Rebuilding the rows
        is deferred to show() while the window is hidden."""
        self._dict_snapshot = list(snapshot or [])
        if self._wants_visible:
            self._rebuild_dictionary()

    def refresh_if_visible(self) -> None:
        """Cheap live update while the window is open: a new dictation
        landed in History (the ("app_refresh",) ui_q message)."""
        if self._wants_visible:
            self._refresh_transcripts()
            self._refresh_footer()

    def set_paused(self, paused: bool) -> None:
        """Mirror of the tray pause state for the footer (called from
        main.py's do_toggle_pause, which runs on the main thread)."""
        self._paused = paused
        self._refresh_footer()

    # ------------------------------------------------------------------ #
    # Window chrome                                                        #
    # ------------------------------------------------------------------ #

    def _apply_icon(self) -> None:
        """Set the title-bar/taskbar icon; never fatal if the file is missing."""
        if not ICON_PATH.exists():
            return
        try:
            self.iconbitmap(str(ICON_PATH))
        except Exception:
            log.debug("could not set app-window icon", exc_info=True)

    def _settle(self) -> None:
        """Post-construction settle: re-assert our icon (ctk's scheduled init
        work overwrites it) and re-hide unless show() ran in the meantime."""
        self._apply_icon()
        if not self._wants_visible:
            self.withdraw()

    # ------------------------------------------------------------------ #
    # Layout                                                              #
    # ------------------------------------------------------------------ #

    def _build_sidebar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=_SIDEBAR, corner_radius=0, width=190)
        bar.grid(row=0, column=0, sticky="nsw")
        bar.grid_propagate(False)

        ctk.CTkLabel(bar, text="Murmur", font=self._font_title,
                     text_color=_TEXT, anchor="w").pack(
            fill="x", padx=20, pady=(24, 0))
        ctk.CTkLabel(bar, text="local dictation", font=self._font_small,
                     text_color=_MUTED, anchor="w").pack(
            fill="x", padx=20, pady=(0, 24))

        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        for key, label in (("transcripts", "Transcripts"),
                           ("dictionary", "Dictionary")):
            btn = ctk.CTkButton(
                bar, text=label, font=self._font, anchor="w", height=36,
                corner_radius=8, fg_color="transparent", hover_color=_CARD,
                text_color=_MUTED,
                command=lambda k=key: self._show_view(k),
            )
            btn.pack(fill="x", padx=12, pady=2)
            self._nav_buttons[key] = btn

    def _show_view(self, key: str) -> None:
        for k, btn in self._nav_buttons.items():
            active = k == key
            btn.configure(fg_color=_CARD if active else "transparent",
                          text_color=_TEXT if active else _MUTED)
        (self._transcripts_frame if key == "transcripts"
         else self._dictionary_frame).tkraise()

    def _build_transcripts_view(self) -> None:
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.grid(row=0, column=1, sticky="nsew", padx=16, pady=(16, 8))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=3)
        frame.grid_rowconfigure(2, weight=0)

        ctk.CTkLabel(frame, text="Transcripts", font=self._font_bold,
                     text_color=_TEXT, anchor="w").grid(
            row=0, column=0, sticky="ew", pady=(0, 8))

        self._t_list = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        self._t_list.grid(row=1, column=0, sticky="nsew")

        detail = ctk.CTkFrame(frame, fg_color=_CARD, corner_radius=10)
        detail.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        detail.grid_columnconfigure(0, weight=1)

        self._detail_box = ctk.CTkTextbox(
            detail, height=96, font=self._font, fg_color="transparent",
            text_color=_TEXT, wrap="word", activate_scrollbars=True,
        )
        self._detail_box.grid(row=0, column=0, columnspan=4, sticky="ew",
                              padx=10, pady=(10, 4))

        self._copy_btn = ctk.CTkButton(
            detail, text="Copy", width=88, height=30, font=self._font,
            fg_color=_ACCENT, hover_color="#5A6BC8",
            command=self._copy_selected,
        )
        self._copy_btn.grid(row=1, column=0, sticky="w", padx=10, pady=(4, 10))

        self._teach_btn = ctk.CTkButton(
            detail, text="Save & teach", width=110, height=30,
            font=self._font, fg_color="#3A4152", hover_color="#4A5266",
            command=self._teach_selected,
        )
        self._teach_btn.grid(row=1, column=1, sticky="w", padx=(0, 10),
                             pady=(4, 10))

        self._t_feedback = ctk.CTkLabel(detail, text="", font=self._font_small,
                                        text_color=_MUTED, anchor="w")
        self._t_feedback.grid(row=1, column=2, sticky="ew", padx=(0, 4),
                              pady=(4, 10))

        ctk.CTkLabel(
            detail, text="edit the text above, then Save & teach",
            font=self._font_small, text_color=_MUTED, anchor="e",
        ).grid(row=1, column=3, sticky="e", padx=(0, 12), pady=(4, 10))

        self._transcripts_frame = frame

    def _build_dictionary_view(self) -> None:
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.grid(row=0, column=1, sticky="nsew", padx=16, pady=(16, 8))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(frame, text="Dictionary", font=self._font_bold,
                     text_color=_TEXT, anchor="w").grid(
            row=0, column=0, sticky="ew", pady=(0, 8))

        self._d_list = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        self._d_list.grid(row=1, column=0, sticky="nsew")

        self._dictionary_frame = frame

    def _build_footer(self) -> None:
        footer = ctk.CTkFrame(self, fg_color=_SIDEBAR, corner_radius=0,
                              height=34)
        footer.grid(row=1, column=0, columnspan=2, sticky="ew")
        footer.grid_propagate(False)
        footer.grid_columnconfigure(1, weight=1)

        model = str(self._config.get("model_size", "?"))
        hotkey = str(self._config.get("hotkey", "?")).upper()
        ctk.CTkLabel(
            footer,
            text=(f"model {model}   ·   hold {hotkey} to dictate"
                  "   ·   Esc cancels"),
            font=self._font_small, text_color=_MUTED, anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=16, pady=6)

        self._status_lbl = ctk.CTkLabel(footer, text="", font=self._font_small,
                                        text_color=_MUTED, anchor="e")
        self._status_lbl.grid(row=0, column=2, sticky="e", padx=16, pady=6)

    # ------------------------------------------------------------------ #
    # Transcripts internals                                               #
    # ------------------------------------------------------------------ #

    def _refresh_transcripts(self) -> None:
        for child in self._t_list.winfo_children():
            child.destroy()
        self._t_rows = []
        self._selected = None
        self._t_feedback.configure(text="")

        all_entries = self._history.entries()  # newest first, locked read
        self._entries = all_entries[:_MAX_LIST_ROWS]
        self._all_count = len(all_entries)
        self._today_count = self._count_today(all_entries)

        if not self._entries:
            hotkey = str(self._config.get("hotkey", "?")).upper()
            ctk.CTkLabel(
                self._t_list, font=self._font, text_color=_MUTED,
                text=f"no transcripts yet — hold {hotkey} and speak",
            ).pack(pady=32)
        for i, entry in enumerate(self._entries):
            self._t_rows.append(self._make_transcript_row(i, entry))
        if len(all_entries) > len(self._entries):
            ctk.CTkLabel(
                self._t_list, font=self._font_small, text_color=_MUTED,
                text=(f"showing latest {len(self._entries)} of "
                      f"{len(all_entries)} — full log in data\\history.jsonl"),
            ).pack(pady=8)

        self._set_detail_text("")
        if self._entries:
            self._select(0)  # newest preselected: open -> Copy flows

    def _make_transcript_row(self, i: int, entry: dict) -> ctk.CTkFrame:
        row = ctk.CTkFrame(self._t_list, fg_color=_CARD, corner_radius=8)
        row.pack(fill="x", padx=4, pady=3)
        row.grid_columnconfigure(2, weight=1)

        when = ctk.CTkLabel(row, text=_fmt_when(str(entry.get("ts", ""))),
                            font=self._font_small, text_color=_MUTED,
                            width=110, anchor="w")
        when.grid(row=0, column=0, padx=(12, 8), pady=8, sticky="w")

        outcome = str(entry.get("outcome", "?"))
        bg, fg = _OUTCOME_COLORS.get(outcome, _OUTCOME_FALLBACK)
        badge = ctk.CTkLabel(row, text=outcome, font=self._font_small,
                             text_color=fg, fg_color=bg, corner_radius=6,
                             padx=8, width=104)
        badge.grid(row=0, column=1, padx=(0, 10), pady=8)

        text = ctk.CTkLabel(row, text=_preview(str(entry.get("text", ""))),
                            font=self._font, text_color=_TEXT, anchor="w",
                            justify="left")
        text.grid(row=0, column=2, padx=(0, 12), pady=8, sticky="ew")

        for w in (row, when, badge, text):
            w.bind("<Button-1>", lambda _e, ix=i: self._select(ix))
        return row

    def _select(self, ix: int) -> None:
        if not (0 <= ix < len(self._entries)):
            return
        if self._selected is not None and self._selected < len(self._t_rows):
            self._t_rows[self._selected].configure(fg_color=_CARD)
        self._selected = ix
        self._t_rows[ix].configure(fg_color=_CARD_SELECTED)
        self._set_detail_text(str(self._entries[ix].get("text", "")))
        self._t_feedback.configure(text="")

    def _set_detail_text(self, text: str) -> None:
        self._detail_box.delete("1.0", "end")
        self._detail_box.insert("1.0", text)

    def _copy_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        text = str(entry.get("text", ""))
        try:
            # User-initiated overwrite — that's the point here, unlike the
            # paste path's snapshot/restore gymnastics.
            set_clipboard_text(text)
        except Exception:  # ClipboardLockedError et al — never crash the UI
            log.exception("app window: copy to clipboard failed")
            self._t_feedback.configure(text="copy failed — clipboard busy")
            return
        self._t_feedback.configure(text=f"copied ({len(text)} chars)")
        log.info("app window: copied %d chars to clipboard", len(text))

    def _teach_selected(self) -> None:
        """Post the (original, edited) pair to the WORKER — extraction,
        dictionary mutation and 'Learned:' toasts all happen there
        (single-writer). History itself stays immutable."""
        entry = self._selected_entry()
        if entry is None:
            return
        original = str(entry.get("text", ""))
        edited = self._detail_box.get("1.0", "end-1c").strip()
        if not edited or edited == original.strip():
            self._t_feedback.configure(text="no changes to teach")
            return
        self._post_job(("teach", original, edited))
        self._t_feedback.configure(text="teaching — watch for Learned: toasts")
        log.info("app window: posted teach job (%d -> %d chars)",
                 len(original), len(edited))

    def _selected_entry(self) -> dict | None:
        if self._selected is None or self._selected >= len(self._entries):
            self._t_feedback.configure(text="nothing selected")
            return None
        return self._entries[self._selected]

    # ------------------------------------------------------------------ #
    # Dictionary internals — renders SNAPSHOTS, posts jobs, never mutates #
    # ------------------------------------------------------------------ #

    def _rebuild_dictionary(self) -> None:
        for child in self._d_list.winfo_children():
            child.destroy()
        self._d_switches = {}
        if not self._dict_snapshot:
            ctk.CTkLabel(
                self._d_list, font=self._font, text_color=_MUTED,
                text=("dictionary is empty — fix a word after dictating "
                      "(or Save & teach a transcript) and it learns"),
            ).pack(pady=32)
            return
        for entry in self._dict_snapshot:
            self._make_dictionary_row(entry)

    def _make_dictionary_row(self, entry: dict) -> None:
        canonical = str(entry.get("canonical", ""))
        row = ctk.CTkFrame(self._d_list, fg_color=_CARD, corner_radius=8)
        row.pack(fill="x", padx=4, pady=3)
        row.grid_columnconfigure(0, weight=1)

        name = ctk.CTkLabel(row, text=canonical, font=self._font_bold,
                            text_color=_TEXT, anchor="w")
        name.grid(row=0, column=0, padx=(12, 8), pady=(8, 0), sticky="ew")

        misheard = ", ".join(entry.get("misheard") or []) or "—"
        counts = (f"heard as: {misheard}    ·    {entry.get('source', '?')}"
                  f"    ·    corrected ×{entry.get('times_corrected', 0)}"
                  f"    ·    applied ×{entry.get('times_applied', 0)}")
        meta = ctk.CTkLabel(row, text=counts, font=self._font_small,
                            text_color=_MUTED, anchor="w")
        meta.grid(row=1, column=0, padx=(12, 8), pady=(0, 8), sticky="ew")

        switch = ctk.CTkSwitch(
            row, text="", width=44, progress_color=_ACCENT,
            command=lambda c=canonical: self._on_toggle(c),
        )
        # Set the initial state WITHOUT firing the command.
        if entry.get("enabled", True):
            switch.select()
        else:
            switch.deselect()
        switch.grid(row=0, column=1, rowspan=2, padx=(0, 4))
        self._d_switches[canonical] = switch

        delete = ctk.CTkButton(
            row, text="Delete", width=64, height=26, font=self._font_small,
            fg_color="transparent", border_width=1, border_color="#4A3030",
            text_color="#C97B7B", hover_color=_DANGER_HOVER,
            command=lambda c=canonical: self._on_delete(c),
        )
        delete.grid(row=0, column=2, rowspan=2, padx=(0, 12))

    def _on_toggle(self, canonical: str) -> None:
        switch = self._d_switches.get(canonical)
        enabled = bool(switch.get()) if switch is not None else True
        self._post_job(("dict_toggle", canonical, enabled))
        log.info("app window: posted dict_toggle %r -> %s", canonical, enabled)

    def _on_delete(self, canonical: str) -> None:
        self._post_job(("dict_delete", canonical))
        log.info("app window: posted dict_delete %r", canonical)

    # ------------------------------------------------------------------ #
    # Footer internals                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _count_today(entries: list[dict]) -> int:
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        return sum(1 for e in entries
                   if str(e.get("ts", "")).startswith(today))

    def _refresh_footer(self) -> None:
        if not getattr(self, "_status_lbl", None):
            return
        n = getattr(self, "_today_count", None)
        if n is None:
            n = self._count_today(self._history.entries())
            self._today_count = n
        status = "paused" if self._paused else "live"
        dot = "⏸" if self._paused else "●"
        color = "#E5C07B" if self._paused else "#7BD88F"
        self._status_lbl.configure(
            text=f"{dot} {status}    ·    {n} dictation"
                 f"{'s' if n != 1 else ''} today",
            text_color=color if self._paused else _MUTED,
        )
