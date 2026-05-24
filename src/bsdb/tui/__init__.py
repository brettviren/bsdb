"""Textual TUI for live monitoring of remote tmux sessions."""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, Static

from bsdb.tmux import SessionInfo
from bsdb.tmux import connection as tmux_connection
from bsdb.tmux import launch as tmux_launch
from bsdb.tmux import list as tmux_list
import bsdb.tmux.monitor as monitor

log = logging.getLogger(__name__)

# Sum of the fixed column widths (bullet + remote + window + cmd + status).
# Used to compute how many characters the Message column can fill.
_FIXED_COL_TOTAL = 1 + 20 + 22 + 14 + 24  # = 81
_MSG_COL_MIN = 10

# terminal emulators tried in order when not inside tmux; each entry is
# (binary, extra_args_before_cmd).
_TERMINAL_LAUNCHERS = [
    ("kitty", ["--"]),
    ("wezterm", ["start", "--"]),
    ("alacritty", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("konsole", ["-e"]),
    ("xterm", ["-e"]),
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_target(arg: str) -> tuple[Optional[str], Optional[str]]:
    """'user@host' → (user@host, None);  'user@host:sess' → (user@host, sess)."""
    if "@" in arg:
        at = arg.index("@")
        after = arg[at + 1:]
        if ":" in after:
            sep = after.index(":")
            return arg[: at + 1 + sep], after[sep + 1:] or None
        return arg, None
    if ":" in arg:
        remote, session = arg.split(":", 1)
        return remote or None, session or None
    return arg or None, None


def _session_key(remote: Optional[str], session_id: str) -> str:
    return f"{remote or 'local'}:{session_id}"


def _window_key(remote: Optional[str], session_id: str, window_id: str) -> str:
    return f"{remote or 'local'}:{session_id}:{window_id}"


def _make_status(info: SessionInfo) -> tuple[Text, str]:
    """Return (coloured bullet, status string) for a SessionInfo row."""
    now = datetime.now(tz=timezone.utc)
    idle = max(0.0, (now - info.activity_at).total_seconds())
    if idle < 60:
        idle_str = f"{int(idle)}s"
    elif idle < 3600:
        idle_str = f"{int(idle // 60)}m"
    else:
        h, m = int(idle // 3600), int((idle % 3600) // 60)
        idle_str = f"{h}h{m:02d}m"

    if info.attached > 0:
        n = info.attached
        return (
            Text("●", style="bold green"),
            f"{idle_str}  {n} client{'s' if n != 1 else ''}",
        )
    if idle < 60:
        return Text("●", style="bold yellow"), idle_str
    if idle < 3600:
        return Text("●", style="bold red"), idle_str
    return Text("○", style="dim red"), idle_str


def _spawn_terminal(cmd: list[str]) -> None:
    """Open a new terminal window (or local tmux window) running *cmd*."""
    if os.environ.get("TMUX"):
        subprocess.Popen(["tmux", "new-window", shlex.join(cmd)])
        return
    env_term = os.environ.get("TERMINAL")
    if env_term and shutil.which(env_term):
        subprocess.Popen([env_term, "-e"] + cmd)
        return
    for binary, prefix in _TERMINAL_LAUNCHERS:
        if shutil.which(binary):
            subprocess.Popen([binary] + prefix + cmd)
            return
    log.warning("no terminal emulator found; set $TERMINAL")


def _msg_first_line(message: Optional[str], max_chars: int = _FIXED_COL_TOTAL) -> str:
    """First line of *message* truncated to *max_chars* characters with '…' if cut."""
    if not message:
        return ""
    line = message.splitlines()[0]
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 1] + "…"


# ---------------------------------------------------------------------------
# message viewer modal
# ---------------------------------------------------------------------------

class _MessageScreen(ModalScreen):
    """Full-message viewer for the selected session row."""

    BINDINGS = [
        Binding("q", "close", "Close"),
        Binding("escape", "close", "Close"),
    ]

    DEFAULT_CSS = """
    _MessageScreen {
        align: center middle;
    }
    _MessageScreen > Vertical {
        width: 80%;
        height: 70%;
        max-width: 100;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    _MessageScreen Label.title {
        text-style: bold;
        margin-bottom: 1;
    }
    _MessageScreen ScrollableContainer {
        height: 1fr;
        border: solid $panel;
        padding: 0 1;
    }
    """

    def __init__(self, info: SessionInfo) -> None:
        super().__init__()
        self._info = info

    def compose(self) -> ComposeResult:
        label = f"{self._info.session_name}:{self._info.window_id}"
        if self._info.remote:
            label = f"{self._info.remote}  {label}"
        with Vertical():
            yield Label(label, classes="title")
            with ScrollableContainer():
                yield Static(Text(self._info.message or ""))

    def action_close(self) -> None:
        self.dismiss()


# ---------------------------------------------------------------------------
# add-target modal
# ---------------------------------------------------------------------------

class _AddTargetScreen(ModalScreen):
    """Modal prompt: enter a remote target to start monitoring."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    _AddTargetScreen {
        align: center middle;
    }
    _AddTargetScreen > Vertical {
        width: 62;
        height: auto;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Add remote target")
            yield Label("[dim]Format:  user@host   or   user@host:session-name[/]")
            yield Input(placeholder="user@host", id="ti")

    def on_mount(self) -> None:
        self.query_one("#ti", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# new-session modal
# ---------------------------------------------------------------------------

class _NewSessionScreen(ModalScreen):
    """Modal form: launch a new tmux session on a remote."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    _NewSessionScreen {
        align: center middle;
    }
    _NewSessionScreen > Vertical {
        width: 64;
        height: auto;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    """

    def __init__(self, default_remote: Optional[str] = None) -> None:
        super().__init__()
        self._default_remote = default_remote or ""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("New tmux session")
            yield Label("[dim]Remote (blank = local)[/]")
            yield Input(placeholder="user@host", id="ns-remote")
            yield Label("[dim]Session name (blank = auto-generated)[/]")
            yield Input(placeholder="auto-generated", id="ns-name")
            yield Label("[dim]Command (blank = $SHELL)[/]")
            yield Input(placeholder="$SHELL", id="ns-cmd")
            yield Label("[dim][Tab] next field  ·  [Enter] launch  ·  [Esc] cancel[/]")

    def on_mount(self) -> None:
        remote_input = self.query_one("#ns-remote", Input)
        remote_input.value = self._default_remote
        remote_input.focus()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        remote = self.query_one("#ns-remote", Input).value.strip() or None
        name = self.query_one("#ns-name", Input).value.strip() or None
        cmd = self.query_one("#ns-cmd", Input).value.strip() or "$SHELL"
        self.dismiss({"remote": remote, "name": name, "cmd": cmd})

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# main app
# ---------------------------------------------------------------------------

class BsdbApp(App):
    """bsdb TUI — live status monitor for remote tmux sessions."""

    TITLE = "bsdb"
    SUB_TITLE = "tmux session monitor"

    CSS = """
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("a", "add_target", "Add"),
        Binding("n", "new_session", "New"),
        Binding("v", "visit", "Visit"),
        Binding("t", "terminal", "Terminal"),
        Binding("m", "message", "Message"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
        Binding("j", "nav_down", "Down", show=False),
        Binding("k", "nav_up", "Up", show=False),
        Binding("ctrl+n", "nav_down", "Down", show=False),
        Binding("ctrl+m", "command_palette", "Commands", show=False),
        Binding("ctrl+p", "nav_up", "Up", show=False, priority=True),
    ]

    def __init__(
        self,
        targets: list[str],
        dwell: int = 30,
        interval: int = 5,
    ) -> None:
        super().__init__()
        self._init_targets = targets
        self._dwell = dwell
        self._interval = interval
        # session-level keys (_session_key) with a running loop worker
        self._watching: set[str] = set()
        # window-level row keys (_window_key) currently in the DataTable
        self._table_rows: set[str] = set()
        # latest SessionInfo per window key
        self._infos: dict[str, SessionInfo] = {}
        # pre_key (worker key) → set of window keys; used for cleanup on teardown
        self._group_rows: dict[str, set[str]] = {}
        # remotes used for discovery (no session filter); refreshed on 'r'
        self._discovery_remotes: list[Optional[str]] = []
        # ColumnKey for the Message column (set in compose); current display width
        self._msg_col_key = None
        self._msg_col_width: int = _MSG_COL_MIN

    # ── layout ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        t = DataTable(cursor_type="row", id="sessions", zebra_stripes=True)
        t.add_column("", width=1, key="bullet")
        t.add_column("Remote", width=20, key="remote")
        t.add_column("Window", width=22, key="window")
        t.add_column("Command", width=14, key="cmd")
        t.add_column("Idle / Status", width=24, key="status")
        self._msg_col_key = t.add_column("Message", width=28, key="message")
        yield t
        yield Footer()

    def on_mount(self) -> None:
        self._set_msg_col_width(self.size.width)
        targets = self._init_targets or [""]
        for arg in targets:
            remote, session = _parse_target(arg)
            if session:
                self.run_worker(
                    self._watch_named(remote, session), exclusive=False
                )
            else:
                self._discovery_remotes.append(remote)
                self.run_worker(self._discover(remote), exclusive=False)
        # refresh status-light colours between monitor events
        self.set_interval(10, self._tick_status)

    def on_resize(self, event) -> None:
        self._set_msg_col_width(event.size.width)

    # ── discovery ─────────────────────────────────────────────────────────

    async def _discover(self, remote: Optional[str]) -> None:
        """Call tmux_list and start a watch worker for each session found."""
        try:
            sessions = await asyncio.to_thread(tmux_list, remote)
        except Exception as exc:
            self.notify(
                f"Cannot list sessions on {remote or 'local'}: {exc}",
                severity="error",
            )
            return
        for info in sessions:
            self._ensure_watching(info)
        if not sessions:
            self.notify(
                f"No tmux sessions found on {remote or 'local'}",
                severity="information",
            )

    async def _watch_named(self, remote: Optional[str], session_name: str) -> None:
        """Resolve session by name via list(), then hand off to loop worker."""
        try:
            sessions = await asyncio.to_thread(tmux_list, remote)
        except Exception:
            sessions = []
        match = next((s for s in sessions if s.session_name == session_name), None)
        if match:
            self._ensure_watching(match)
        else:
            # Let loop() try to resolve the name directly.
            key = f"{remote or 'local'}:name:{session_name}"
            if key not in self._watching:
                self._watching.add(key)
                self.run_worker(
                    self._loop_worker(session_name, remote=remote, pre_key=key),
                    exclusive=False,
                )

    def _ensure_watching(self, info: SessionInfo) -> None:
        key = _session_key(info.remote, info.session_id)
        if key not in self._watching:
            self._watching.add(key)
            self.run_worker(
                self._loop_worker(info, pre_key=key), exclusive=False
            )

    # ── monitor worker ────────────────────────────────────────────────────

    async def _loop_worker(
        self,
        session: SessionInfo | str,
        remote: Optional[str] = None,
        pre_key: Optional[str] = None,
    ) -> None:
        try:
            async with monitor.loop(
                session,
                remote=remote,
                dwell=self._dwell,
                interval=self._interval,
            ) as events:
                try:
                    async for info in events:
                        self._update_session(info, group_key=pre_key or "")
                except asyncio.CancelledError:
                    # Python 3.12 tracks cancellation requests with a counter.
                    # Catching CancelledError does NOT decrement it, so each
                    # subsequent await inside monitor.loop()'s finally block is
                    # immediately re-cancelled, leaving the subprocess transport
                    # open and causing "Event loop is closed" on GC cleanup.
                    #
                    # Textual also calls task.cancel() twice: once from
                    # workers.cancel_all() and again from asyncio.run()'s
                    # _cancel_all_tasks.  Drain all pending cancellations so
                    # the loop() cleanup awaits can run to completion.
                    t = asyncio.current_task()
                    if t is not None:
                        while t.cancelling():
                            t.uncancel()
        except Exception as exc:
            log.debug("loop_worker(%r): %s", pre_key, exc)
        finally:
            if pre_key:
                self._mark_gone(pre_key)

    # ── table helpers ─────────────────────────────────────────────────────

    def _update_session(self, info: SessionInfo, group_key: str = "") -> None:
        wkey = _window_key(info.remote, info.session_id, info.window_id)
        self._infos[wkey] = info
        if group_key:
            self._group_rows.setdefault(group_key, set()).add(wkey)
        bullet, status = _make_status(info)
        window_label = f"{info.session_name}:{info.window_id}"
        msg_preview = _msg_first_line(info.message, self._msg_col_width)
        table = self.query_one("#sessions", DataTable)
        if wkey in self._table_rows:
            table.update_cell(wkey, "bullet", bullet, update_width=False)
            table.update_cell(wkey, "window", window_label, update_width=False)
            table.update_cell(wkey, "cmd", info.cmd, update_width=False)
            table.update_cell(wkey, "status", status, update_width=False)
            table.update_cell(wkey, "message", msg_preview, update_width=False)
        else:
            self._table_rows.add(wkey)
            table.add_row(
                bullet,
                info.remote or "local",
                window_label,
                info.cmd,
                status,
                msg_preview,
                key=wkey,
            )

    def _mark_gone(self, pre_key: str) -> None:
        self._watching.discard(pre_key)
        window_keys = self._group_rows.pop(pre_key, set())
        for wk in window_keys:
            self._infos.pop(wk, None)
            self._table_rows.discard(wk)
            if not self.is_running:
                continue
            try:
                self.query_one("#sessions", DataTable).remove_row(wk)
            except Exception:
                pass

    def _set_msg_col_width(self, terminal_width: int) -> None:
        """Resize the Message column to fill available terminal space."""
        # Subtract 1 extra char so the vertical scrollbar never clips the column.
        new_width = max(_MSG_COL_MIN, terminal_width - _FIXED_COL_TOTAL - 1)
        if new_width == self._msg_col_width:
            return
        self._msg_col_width = new_width
        table = self.query_one("#sessions", DataTable)
        if self._msg_col_key is not None:
            table.columns[self._msg_col_key].width = new_width
        for wkey, info in list(self._infos.items()):
            if wkey in self._table_rows:
                table.update_cell(
                    wkey, "message",
                    _msg_first_line(info.message, new_width),
                    update_width=False,
                )
        table.refresh(layout=True)

    def _tick_status(self) -> None:
        """Refresh status cells so idle times stay current between loop events."""
        table = self.query_one("#sessions", DataTable)
        for key, info in list(self._infos.items()):
            if key in self._table_rows:
                bullet, status = _make_status(info)
                table.update_cell(key, "bullet", bullet, update_width=False)
                table.update_cell(key, "status", status, update_width=False)

    def _cursor_info(self) -> Optional[SessionInfo]:
        table = self.query_one("#sessions", DataTable)
        if not table.row_count:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            return self._infos.get(str(cell_key.row_key.value))
        except Exception:
            return None

    # ── key actions ───────────────────────────────────────────────────────

    def action_nav_down(self) -> None:
        self.query_one("#sessions", DataTable).action_cursor_down()

    def action_nav_up(self) -> None:
        self.query_one("#sessions", DataTable).action_cursor_up()

    def action_add_target(self) -> None:
        def _cb(result: Optional[str]) -> None:
            if not result:
                return
            remote, session = _parse_target(result)
            if session:
                self.run_worker(
                    self._watch_named(remote, session), exclusive=False
                )
            else:
                if remote not in self._discovery_remotes:
                    self._discovery_remotes.append(remote)
                self.run_worker(self._discover(remote), exclusive=False)

        self.push_screen(_AddTargetScreen(), _cb)

    def action_refresh(self) -> None:
        for remote in self._discovery_remotes:
            self.run_worker(self._discover(remote), exclusive=False)
        self.notify("Refreshing…")

    def action_new_session(self) -> None:
        info = self._cursor_info()
        self.push_screen(
            _NewSessionScreen(default_remote=info.remote if info else None),
            self._on_new_session,
        )

    def _on_new_session(self, result: Optional[dict]) -> None:
        if result is None:
            return
        self.run_worker(
            self._launch_session(result["remote"], result["name"], result["cmd"]),
            exclusive=False,
        )

    async def _launch_session(
        self, remote: Optional[str], name: Optional[str], cmd: str
    ) -> None:
        try:
            info = await asyncio.to_thread(tmux_launch, cmd, name=name, remote=remote)
        except Exception as exc:
            self.notify(f"Launch failed: {exc}", severity="error")
            return
        self._ensure_watching(info)
        self.notify(f"Launched {info.session_name} on {info.remote or 'local'}")

    def action_message(self) -> None:
        info = self._cursor_info()
        if info is None:
            self.notify("No session selected", severity="warning")
            return
        if not info.message:
            self.notify("No message for this session", severity="information")
            return
        self.push_screen(_MessageScreen(info))

    def action_visit(self) -> None:
        info = self._cursor_info()
        if info is None:
            self.notify("No session selected", severity="warning")
            return
        cmd = tmux_connection(info, info.remote, window=info.window_id)
        with self.suspend():
            subprocess.run(cmd)

    def action_terminal(self) -> None:
        info = self._cursor_info()
        if info is None:
            self.notify("No session selected", severity="warning")
            return
        _spawn_terminal(tmux_connection(info, info.remote, window=info.window_id))
