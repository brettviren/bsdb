# bsdb — development notes for Claude

## What this project is

`bsdb` is a Python package (Python 3.12, `uv` build, `src/` layout) for launching,
monitoring, and re-entering terminal processes in tmux on local or remote hosts via
SSH, with state tracked in a central SQLite database and multiple UI layers (CLI,
TUI, GUI, web) to be added later.

## Development environment

The project uses `uv` with a virtualenv at `.venv/`.  Always invoke Python and
tools through the venv so the `bsdb` package is on the path:

```
.venv/bin/python -c "..."
.venv/bin/pytest test/
.venv/bin/bsdb ...
```

The system `python` (e.g. `/usr/bin/python`) does **not** have `bsdb` on its
path and will raise `ModuleNotFoundError`.

## Current state

Stage 1 is complete: `bsdb.tmux`, a package that wraps tmux and SSH with
no dependencies on the future database or UI layers.  The package also includes
`bsdb.tmux.monitor` for async push-based session monitoring.

### `src/bsdb/tmux/__init__.py`

Public API:

| Symbol | Description |
|--------|-------------|
| `SessionInfo` | Dataclass carrying all identifiers for a tmux session |
| `launch(cmd, name, cwd, remote)` | Start `cmd` in a detached tmux session; returns `SessionInfo` |
| `list(remote)` | Return `list[SessionInfo]` for all sessions on a host |
| `attach(session, remote)` | Attach to a session in the current terminal; returns exit code |
| `connection(session, remote)` | Return the `list[str]` command that `attach()` would run, without running it |

Private helpers (tested directly):

| Symbol | Description |
|--------|-------------|
| `_resolve_remote(remote)` | Canonicalise a remote string; `None`/`""`/`"localhost"` → `None` |
| `_resolve_session_spec(session, remote)` | Resolve `(session, remote)` → `(tmux_target, canonical_remote)` |
| `_run(args, remote)` | Run args locally or pipe via `$SHELL -l` stdin to remote |

### `SessionInfo` fields

```python
session_name: str            # human-readable name set at launch
session_id: str              # tmux-internal stable ID, e.g. "$0"
window_id: str               # first window ID, e.g. "@0"
pane_id: str                 # first pane ID, e.g. "%0"
pane_pid: int                # PID of process running in the first pane
remote: Optional[str]        # SSH target; None = local
created_at: datetime         # UTC
cmd: str                     # normalised shell command string
cwd: Optional[str]           # working directory, or None
attached: int                # number of clients currently attached
activity_at: datetime        # UTC time of last pane activity
last_attached_at: Optional[datetime]  # UTC time a client last attached; None if never
many_attached: bool          # True when more than one client is attached
```

### `src/bsdb/tmux/monitor.py`

Async push-based monitoring via tmux hooks.  Each public function is an async
context manager that yields `AsyncIterator[SessionInfo]`.  Hooks are installed
on entry and torn down on exit (including via `asyncio.CancelledError`).

Common signature:

```python
async with monitor.METHOD(session, remote=None, dwell=10) as events:
    async for info in events:
        ...
```

`dwell` (seconds, default 10) is passed to tmux `monitor-silence` so that
`alert-silence` fires after that many seconds of pane inactivity.  The three
hooks installed on the target session are: `alert-bell`, `alert-silence`,
`alert-activity`.  (`session-activity` and `window-activity` are not valid tmux
hook names; the correct hook for pane/window activity is `alert-activity`.)
Each hook captures the current
session state via `tmux list-panes -F ...` and delivers it through the chosen
transport.

| Function | Transport |
|----------|-----------|
| `pipe(session, remote, dwell)` | Named FIFO in `$XDG_RUNTIME_DIR`; `cat` streams it back over SSH stdout |
| `socat(session, remote, dwell)` | Unix domain socket; `socat UNIX-LISTEN` streams it back over SSH stdout |
| `listen(session, remote, dwell)` | SSH `-R` reverse port forward; asyncio TCP server receives locally |

The shared format string written by every hook is a tab-separated subset of
`list-panes -F` fields (12 fields, same order as `list()` but without
`window_index`/`pane_index` filter columns).  `_parse_line()` converts one
such line to a `SessionInfo`.

Tmux options set on the target session at startup: `monitor-silence $dwell`,
`silence-action other`, `monitor-activity on`.  These are left in place after
cleanup (harmless without hooks).

### `src/bsdb/__main__.py`

Click CLI entry point (`bsdb`). Subcommands:

| Command | Description |
|---------|-------------|
| `bsdb launch [options] CMD...` | Launch CMD; prints `[remote:]name  (pid N)` |
| `bsdb attach [options] TARGET` | Attach to TARGET in current terminal |
| `bsdb connection [options] TARGET` | Print the attach command for copy-paste use |
| `bsdb list REMOTE...` | List sessions on each REMOTE (alias: `ls`) |
| `bsdb monitor [options] TARGET` | Stream session events; Ctrl-C for clean shutdown |

TARGET may be a session name, a tmux ID (`$N`), or the `remote:name` string
printed by `launch`.  `--remote / -r` overrides any remote embedded in TARGET.

`launch` uses `context_settings={"allow_interspersed_args": False}` so flags in
CMD (e.g. `emacs -nw`) are not consumed by Click.

`monitor` options:

| Option | Default | Description |
|--------|---------|-------------|
| `--method / -m` | `pipe` | Monitoring transport (`pipe`, `socat`, `listen`) |
| `--dwell / -d` | `10` | Silence timeout in seconds |

The `monitor` command body is `asyncio.run(_run())` with a SIGINT handler that
sets an `asyncio.Event`; the event unblocks the main coroutine, which exits the
`async with` block to trigger transport teardown before the process exits.

## Architecture decisions worth keeping

**`$SHELL -l` via stdin for non-interactive SSH** — `_run()` pipes commands to
`$SHELL -l` on stdin rather than passing them as an SSH argument.  This solves two
problems at once: (1) login profiles are sourced so tools like tmux installed via
Homebrew, nix, or conda are on PATH; (2) no nested shell quoting is needed because
the command arrives on stdin, not as a shell argument.  The remote's login shell is
used (`$SHELL`) rather than hardcoding `bash`, which matters on macOS where the
default shell is zsh.

**`ssh -t … $SHELL -l -c <quoted>` for interactive attach** — `attach()` needs a
PTY, which requires `ssh -t`.  Once `-t` is in use, stdin is the PTY and the stdin
trick can't be used; instead `$SHELL -l -c` with a `shlex.quote`-d command is the
right approach.

**`from __future__ import annotations`** — `list` is shadowed at module scope by
our own `list()` function.  The future-annotations import makes all annotations
lazily-evaluated strings, so `list[str]` in type hints works.  The same shadowing
requires `not isinstance(cmd, str)` instead of `isinstance(cmd, list)` for the
runtime check in `launch()`, and `[*seen.values()]` instead of `list(seen.values())`
in `list()`.

**Tab separator in `list-panes` format** — `\t` is used between fields (not `|`)
because Unix paths can contain `|`.  `str.splitlines()` is used instead of
`strip().split("\n")` because `strip()` removes trailing tabs, which breaks empty
`pane_current_path` fields.

**`last_attached_at` is `Optional[datetime]`** — tmux reports an empty string for
`#{session_last_attached}` when no client has ever attached (common for freshly
launched detached sessions).  Parsed as `None`.

**`bsdb.tmux` is a package, not a module** — `tmux.py` was promoted to
`tmux/__init__.py` when `monitor.py` was added as a sibling.  All existing
imports (`from bsdb.tmux import ...`) continue to work unchanged.

**Monitor methods use async context managers, not callbacks** — each monitor
function is decorated with `@asynccontextmanager` and yields an
`AsyncIterator[SessionInfo]` backed by an `asyncio.Queue`.  A background task
(`asyncio.create_task`) drains the SSH/TCP stream into the queue; the context
manager's `finally` block cancels the task and runs cleanup.  This keeps the
caller's loop clean and makes SIGINT handling straightforward.

**Named pipe (FIFO) reader must exist before hooks fire** — the `pipe` method
sends one SSH script that both installs hooks and then blocks on `cat $FIFO`.
Because `cat` is the persistent reader, hook writes never block waiting for a
consumer, even when the hooks fire in rapid succession.

**`socat UNIX-LISTEN … fork` is required** — without `fork`, socat exits after
the first hook connection closes the socket.  `fork` keeps the listener alive
for subsequent hook fires.

**`ssh -R` keeps the remote shell alive with `tail -f /dev/null`** — the
`listen` method needs the SSH connection to persist (to maintain the reverse
port forward) after the hook-setup script finishes.  `tail -f /dev/null`
blocks indefinitely without reading stdin, so Python can close stdin (sending
EOF) just like `pipe` and `socat` do.  Keeping stdin open and using `read`
to block instead caused SSH to suppress stderr output until session teardown —
likely an SSH flow-control interaction tied to the open stdin channel.

**`$XDG_RUNTIME_DIR` for named pipes and sockets** — files are placed in
`${XDG_RUNTIME_DIR:-/tmp/bsdb-run-$(id -u)}/bsdb-{method}-{session_id}.{ext}`
following Linux standards for user-owned runtime state.  The fallback
`/tmp/bsdb-run-UID` covers systems without a running logind.

## Test layout

`test/test_tmux.py` — 49 tests across five classes (all pass after the
`tmux.py` → `tmux/__init__.py` refactor; no test changes needed):

| Class | What it covers |
|-------|----------------|
| `TestLaunchLocal` | Real tmux sessions launched locally (10 tests) |
| `TestLaunchRemoteLocalhost` | Real sessions via SSH to localhost; skipped if passwordless SSH unavailable (5 tests) |
| `TestAttachTargetResolution` | `attach()` target/remote resolution with mocked subprocess (6 tests) |
| `TestResolveRemote` | `_resolve_remote()` pure-function edge cases (7 tests) |
| `TestList` | `list()` parsing with mocked `_run` (13 tests) |
| `TestConnection` | `connection()` return value for all session/remote forms (9 tests) |

`_make_session_info(**overrides)` in the test file is a helper that builds a
minimal valid `SessionInfo` with sensible defaults; use it in any test that needs
a `SessionInfo` object rather than constructing one inline.

## Known limitation: tmux hook alerts require an attached client

`alert-activity`, `alert-silence`, and `alert-bell` hooks only fire when tmux
processes pending alerts.  tmux defers alert processing until a client is
attached to the session.  All three monitor methods (`pipe`, `socat`, `listen`)
share this limitation: they go silent when the monitored session is detached,
which is exactly the common case for long-running background jobs.

### Alternative monitoring approaches for detached sessions

**`tmux pipe-pane` (most promising)**  
`tmux pipe-pane -t TARGET 'command'` continuously pipes all pane stdout to
`command` regardless of client attachment.  bsdb could set this up at monitor
entry, sending each line (or a debounced summary) through a FIFO/socket/TCP
tunnel.  Silence detection needs a separate timeout layer on the Python side
(track the last-received-line timestamp and emit a silence event after dwell
seconds of quiet).  Clean teardown: `tmux pipe-pane -t TARGET` with no
argument removes the pipe.

**Polling `tmux list-panes` via SSH**  
Call `tmux list-panes -F '...'` every N seconds and compare the
`session_activity` timestamp between polls.  Simple, zero new dependencies,
works everywhere.  Latency is the poll interval (acceptable for most use
cases: 5–30 s).  Silence detection is implicit: no timestamp change for N
seconds means silence.  This is the right fallback if push methods prove
unreliable.

**inotify on the pane PTY (Linux only)**  
`tmux display-message -t TARGET -p '#{pane_tty}'` returns the pane's PTY
path (e.g. `/dev/pts/7`).  `inotifywait -m -e modify /dev/pts/7` fires on
every write to the PTY — truly push-based, zero latency, no tmux client
needed.  Limitation: Linux-only (no macOS equivalent); requires knowing the
PTY path before starting; the PTY path changes if tmux is restarted.

**In-session watcher process (most flexible, most invasive)**  
At `bsdb launch` time, start a silent background helper inside the same tmux
session (e.g. in a hidden window) that monitors the pane and pushes events.
For example: `while inotifywait -e modify $PTY; do tmux list-panes ... | nc
...; done`.  This survives tmux client detach/attach cycles and can detect
both activity and silence without any hook machinery.  Trade-off: the
session now has an extra window/pane that bsdb must manage and clean up.

### Recommended path forward

For stage 2, implement the `pipe-pane` approach as the primary push method
and the polling approach as a universal fallback.  The existing `pipe`,
`socat`, and `listen` methods remain useful for attached-session monitoring
(e.g. watching a session you are actively working in) but should be
documented as not firing for detached sessions.

## Planned next stages

2. `bsdb.db` — SQLite tracking module that consumes `SessionInfo` from `bsdb.tmux`
3. CLI, TUI, GUI, and web UI layers on top of the db module

`bsdb.tmux.monitor` (stage 1.5) is implemented.  The three hook-based methods
work for attached sessions; the detached-session limitation above is the primary
open issue before stage 2.
