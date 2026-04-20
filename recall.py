#!/usr/bin/env python3
"""OpenCode Recall — full-text search your OpenCode conversations."""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

# ── Database helpers ──────────────────────────────────────────────────────────

DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def load_sessions(
    conn: sqlite3.Connection,
    scope_dir: Optional[str] = None,
) -> list[dict]:
    """Load all sessions, optionally filtered to a directory scope."""
    query = """
        SELECT s.id, s.title, s.directory, s.time_created, s.time_updated,
               p.worktree as project_path,
               (SELECT count(*) FROM message m WHERE m.session_id = s.id) as msg_count
        FROM session s
        JOIN project p ON s.project_id = p.id
    """
    params: list = []
    if scope_dir:
        query += " WHERE (s.directory = ? OR s.directory LIKE ? OR p.worktree = ?)"
        params = [scope_dir, scope_dir + "/%", scope_dir]
    query += " ORDER BY s.time_updated DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def search_sessions(
    conn: sqlite3.Connection,
    query_text: str,
    scope_dir: Optional[str] = None,
) -> list[dict]:
    """Full-text search across conversation content, return matching sessions."""
    like = f"%{query_text}%"
    query = """
        SELECT DISTINCT s.id, s.title, s.directory, s.time_created, s.time_updated,
               p.worktree as project_path,
               (SELECT count(*) FROM message m WHERE m.session_id = s.id) as msg_count
        FROM session s
        JOIN project p ON s.project_id = p.id
        WHERE (
            s.title LIKE ?
            OR s.id IN (
                SELECT DISTINCT pt.session_id FROM part pt
                WHERE pt.data LIKE ?
            )
        )
    """
    params: list = [like, like]
    if scope_dir:
        query += " AND (s.directory = ? OR s.directory LIKE ? OR p.worktree = ?)"
        params += [scope_dir, scope_dir + "/%", scope_dir]
    query += " ORDER BY s.time_updated DESC LIMIT 100"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def load_conversation(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """Load full conversation for a session as a list of turns."""
    rows = conn.execute(
        """
        SELECT m.id, m.data as msg_data, m.time_created
        FROM message m
        WHERE m.session_id = ?
        ORDER BY m.time_created ASC
        """,
        [session_id],
    ).fetchall()

    turns: list[dict] = []
    for row in rows:
        msg = json.loads(row["msg_data"])
        role = msg.get("role", "unknown")
        msg_id = row["id"]

        parts = conn.execute(
            "SELECT data FROM part WHERE message_id = ? ORDER BY time_created ASC",
            [msg_id],
        ).fetchall()

        texts: list[str] = []
        for p in parts:
            pd = json.loads(p["data"])
            if pd.get("type") == "text" and pd.get("text"):
                texts.append(pd["text"])
            elif pd.get("type") == "tool":
                tool_name = pd.get("tool", "tool")
                state = pd.get("state", {})
                title = state.get("title", "")
                status = state.get("status", "")
                inp = state.get("input", {})
                summary = title or ""
                if not summary and isinstance(inp, dict):
                    # Build a short summary from input
                    for k, v in list(inp.items())[:2]:
                        val = str(v)[:80]
                        summary += f" {k}={val}"
                status_icon = (
                    "✓"
                    if status == "completed"
                    else "…"
                    if status == "running"
                    else "✗"
                    if status == "error"
                    else "·"
                )
                texts.append(f"  ⚙ {tool_name} {status_icon} {summary}".rstrip())

        if texts:
            turns.append(
                {
                    "role": role,
                    "text": "\n".join(texts),
                    "time": row["time_created"],
                }
            )
    return turns


# ── Formatting helpers ────────────────────────────────────────────────────────


def format_time(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000)
    now = datetime.now()
    diff = now - dt
    if diff.days == 0:
        hours = diff.seconds // 3600
        if hours == 0:
            mins = diff.seconds // 60
            if mins == 0:
                return "just now"
            return f"{mins}m ago"
        return f"{hours}h ago"
    elif diff.days == 1:
        return "yesterday"
    elif diff.days < 7:
        return f"{diff.days}d ago"
    elif diff.days < 30:
        return f"{diff.days // 7}w ago"
    elif diff.days < 365:
        return dt.strftime("%b %d")
    else:
        return dt.strftime("%b %d '%y")


def short_dir(directory: str) -> str:
    home = str(Path.home())
    if directory.startswith(home):
        return "~" + directory[len(home) :]
    return directory


# ── Widgets ───────────────────────────────────────────────────────────────────


class SessionItem(ListItem):
    """A single session entry in the list."""

    def __init__(self, session: dict) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        s = self.session
        time_str = format_time(s["time_updated"])
        dir_str = short_dir(s["directory"])
        title = s["title"] or "(untitled)"
        msgs = s["msg_count"]

        yield Static(
            f"[bold #5eead4]{title}[/]\n"
            f"[#6b7280]{dir_str}[/]  "
            f"[#fbbf24]{msgs} msgs[/]  "
            f"[#34d399]{time_str}[/]",
            markup=True,
        )


class ConversationPreview(VerticalScroll):
    """Right panel showing conversation content."""

    def compose(self) -> ComposeResult:
        yield Static(
            "[dim italic]Select a session to preview[/]",
            id="preview-content",
            markup=True,
        )

    def update_preview(self, turns: list[dict], title: str = "") -> None:
        content = self.query_one("#preview-content", Static)
        if not turns:
            content.update("[dim italic]Empty session[/]")
            return

        lines: list[str] = []
        if title:
            lines.append(f"[bold underline #e2e8f0]{title}[/]")
            lines.append("")

        for i, turn in enumerate(turns):
            role = turn["role"]
            text = turn["text"]
            ts = format_time(turn["time"]) if turn.get("time") else ""

            if len(text) > 3000:
                text = text[:3000] + "\n… (truncated)"

            escaped = text.replace("[", "\\[")

            if role == "user":
                lines.append(f"[bold #22d3ee]━━ You [/][dim]{ts}[/]")
                lines.append(f"[#f8fafc]{escaped}[/]")
            else:
                lines.append(f"[bold #c084fc]━━ Assistant [/][dim]{ts}[/]")
                lines.append(f"[#cbd5e1]{escaped}[/]")
            lines.append("")

        content.update("\n".join(lines))
        self.scroll_home(animate=False)


class ScopeIndicator(Static):
    """Shows current scope with a colored indicator."""

    pass


# ── Main App ──────────────────────────────────────────────────────────────────


class RecallApp(App):
    """OpenCode Recall — search and resume your conversations."""

    TITLE = "recall"
    SUB_TITLE = "opencode session search"

    CSS = """
    Screen {
        background: #0f172a;
    }

    Header {
        background: #1e293b;
        color: #e2e8f0;
        dock: top;
        height: 1;
    }

    #toolbar {
        dock: top;
        height: 1;
        background: #1e293b;
        layout: horizontal;
        padding: 0 1;
    }

    #scope-indicator {
        width: auto;
        color: #fbbf24;
        background: #1e293b;
        padding: 0 1;
    }

    #session-count {
        width: auto;
        color: #64748b;
        background: #1e293b;
        padding: 0 1;
    }

    #search-box {
        dock: top;
        height: 3;
        padding: 0 1;
        background: #0f172a;
    }

    #search-input {
        width: 100%;
        border: round #6366f1;
        background: #1e293b;
        color: #f8fafc;
    }

    #search-input:focus {
        border: round #818cf8;
    }

    #main-content {
        layout: horizontal;
        height: 1fr;
    }

    #session-list-container {
        width: 2fr;
        min-width: 35;
        background: #0f172a;
    }

    #session-list {
        height: 1fr;
        background: #0f172a;
        scrollbar-color: #334155;
        scrollbar-color-hover: #475569;
        scrollbar-color-active: #6366f1;
    }

    #session-list > ListItem {
        padding: 0 1;
        height: auto;
        background: #0f172a;
    }

    #session-list > ListItem:hover {
        background: #1e293b;
    }

    #session-list > ListItem.-highlight {
        background: #1e1b4b;
    }

    #divider {
        width: 1;
        background: #334155;
    }

    #preview-panel {
        width: 3fr;
        height: 1fr;
        background: #111827;
        padding: 1 2;
        scrollbar-color: #334155;
        scrollbar-color-hover: #475569;
        scrollbar-color-active: #6366f1;
    }

    #preview-content {
        width: 100%;
    }

    #help-bar {
        dock: bottom;
        height: 1;
        background: #1e293b;
        color: #94a3b8;
        content-align: center middle;
        text-align: center;
    }

    Footer {
        display: none;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("escape", "focus_search", "Back to search", show=False),
        Binding("ctrl+s", "toggle_scope", "Toggle scope", show=True),
        Binding("enter", "resume_session", "Resume", show=True),
        Binding("ctrl+y", "copy_session_id", "Copy ID", show=True),
        Binding("ctrl+e", "toggle_scope", "Scope", show=True),
    ]

    scope_everywhere: reactive[bool] = reactive(True)  # Start everywhere

    def __init__(self) -> None:
        super().__init__()
        self.conn = get_db()
        self.cwd = os.getcwd()
        self.total_count = 0
        self._selected_session: Optional[dict] = None
        self._search_timer: Optional[Timer] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="toolbar"):
            yield ScopeIndicator(id="scope-indicator")
            yield Static(id="session-count")
        with Horizontal(id="search-box"):
            yield Input(
                placeholder="Type to search conversations…",
                id="search-input",
            )
        with Horizontal(id="main-content"):
            with Vertical(id="session-list-container"):
                yield ListView(id="session-list")
            yield Static(id="divider")
            yield ConversationPreview(id="preview-panel")
        yield Static(
            "[#6366f1]↑↓[/] navigate  "
            "[#6366f1]Enter[/] resume  "
            "[#6366f1]Ctrl+Y[/] copy ID  "
            "[#6366f1]Ctrl+E[/] toggle scope  "
            "[#6366f1]Esc[/] quit",
            id="help-bar",
            markup=True,
        )

    def on_mount(self) -> None:
        self.total_count = self.conn.execute("SELECT count(*) FROM session").fetchone()[
            0
        ]
        self._refresh_sessions()
        self.query_one("#search-input", Input).focus()

    @on(Input.Changed, "#search-input")
    def on_search_changed(self, event: Input.Changed) -> None:
        # Debounce: wait 300ms after last keystroke
        if self._search_timer is not None:
            self._search_timer.stop()
        self._search_timer = self.set_timer(
            0.3, lambda: self._refresh_sessions(event.value)
        )

    def _refresh_sessions(self, query: str = "") -> None:
        scope = None if self.scope_everywhere else self.cwd
        if query.strip():
            sessions = search_sessions(self.conn, query.strip(), scope)
        else:
            sessions = load_sessions(self.conn, scope)

        self._selected_session = None

        lv = self.query_one("#session-list", ListView)
        lv.clear()
        for s in sessions:
            lv.append(SessionItem(s))

        # Update toolbar
        scope_label = (
            "[#fbbf24]● everywhere[/]"
            if self.scope_everywhere
            else f"[#34d399]● {short_dir(self.cwd)}[/]"
        )
        try:
            self.query_one("#scope-indicator", ScopeIndicator).update(scope_label)
            self.query_one("#session-count", Static).update(
                f"[#64748b]{len(sessions)}/{self.total_count} sessions[/]"
            )
        except NoMatches:
            pass

    @on(ListView.Highlighted, "#session-list")
    def on_session_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item and isinstance(event.item, SessionItem):
            self._selected_session = event.item.session
            self._load_preview(event.item.session)

    @work(thread=True, exclusive=True)
    def _load_preview(self, session: dict) -> None:
        conn = get_db()  # New connection for this thread
        try:
            turns = load_conversation(conn, session["id"])
            self.call_from_thread(
                self.query_one("#preview-panel", ConversationPreview).update_preview,
                turns,
                session.get("title", ""),
            )
        finally:
            conn.close()

    def action_focus_search(self) -> None:
        search = self.query_one("#search-input", Input)
        if search.has_focus:
            self.exit()
        else:
            search.focus()

    def action_toggle_scope(self) -> None:
        self.scope_everywhere = not self.scope_everywhere
        query = self.query_one("#search-input", Input).value
        self._refresh_sessions(query)
        scope_name = "everywhere" if self.scope_everywhere else short_dir(self.cwd)
        self.notify(f"Scope: {scope_name}", severity="information", timeout=2)

    def action_resume_session(self) -> None:
        if not self._selected_session:
            self.notify("No session selected", severity="warning")
            return
        sid = self._selected_session["id"]
        self.exit(result=("resume", sid))

    def action_copy_session_id(self) -> None:
        if not self._selected_session:
            self.notify("No session selected", severity="warning")
            return
        sid = self._selected_session["id"]
        try:
            import pyperclip

            pyperclip.copy(sid)
            self.notify(f"Copied: {sid}", severity="information", timeout=2)
        except Exception:
            # Fallback: try pbcopy on macOS
            try:
                subprocess.run(["pbcopy"], input=sid.encode(), check=True)
                self.notify(f"Copied: {sid}", severity="information", timeout=2)
            except Exception:
                self.notify(f"ID: {sid}", severity="information", timeout=5)


def main() -> None:
    if not DB_PATH.exists():
        print(f"Error: OpenCode database not found at {DB_PATH}")
        print("Make sure OpenCode has been used at least once.")
        sys.exit(1)

    app = RecallApp()
    result = app.run()

    if result and isinstance(result, tuple) and result[0] == "resume":
        session_id = result[1]
        print(f"\nResuming session: {session_id}")
        try:
            subprocess.run(["opencode", "--session", session_id], check=False)
        except FileNotFoundError:
            print("Could not find 'opencode' command.")
            print(f"Session ID: {session_id}")
            print("Resume manually: opencode --session " + session_id)


if __name__ == "__main__":
    main()
