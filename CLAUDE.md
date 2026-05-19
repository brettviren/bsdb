# bsdb — development notes for Claude

## What this project is

`bsdb` is a Python package (Python 3.12, `uv` build, `src/` layout) for launching,
monitoring, and re-entering terminal processes in tmux on local or remote hosts via
SSH, with state tracked in a central SQLite database and multiple UI layers (CLI,
TUI, GUI, web) to be added later.

## Current state

Stage 1 is complete: `bsdb.tmux`, a primitive module that wraps tmux and SSH with
no dependencies on the future database or UI layers.

### `src/bsdb/tmux.py`

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

### `src/bsdb/__main__.py`

Click CLI entry point (`bsdb`). Subcommands:

| Command | Description |
|---------|-------------|
| `bsdb launch [options] CMD...` | Launch CMD; prints `[remote:]name  (pid N)` |
| `bsdb attach [options] TARGET` | Attach to TARGET in current terminal |
| `bsdb connection [options] TARGET` | Print the attach command for copy-paste use |
| `bsdb list REMOTE...` | List sessions on each REMOTE (alias: `ls`) |

TARGET may be a session name, a tmux ID (`$N`), or the `remote:name` string
printed by `launch`.  `--remote / -r` overrides any remote embedded in TARGET.

`launch` uses `context_settings={"allow_interspersed_args": False}` so flags in
CMD (e.g. `emacs -nw`) are not consumed by Click.

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

## Test layout

`test/test_tmux.py` — 49 tests across five classes:

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

## Planned next stages

2. `bsdb.db` — SQLite tracking module that consumes `SessionInfo` from `bsdb.tmux`
3. CLI, TUI, GUI, and web UI layers on top of the db module
