"""Primitive functions for launching and managing tmux sessions locally or via SSH."""

import shlex
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class SessionInfo:
    """All identifiers needed to reattach to or track a launched tmux session.

    Fields with tmux IDs (session_id, window_id, pane_id) are stable across
    renames and are safe to use with ``-t`` even if the session is renamed.
    """

    session_name: str      # human-readable name set at launch
    session_id: str        # tmux-internal unique ID, e.g. "$0"
    window_id: str         # first window ID, e.g. "@0"
    pane_id: str           # first pane ID, e.g. "%0"
    pane_pid: int          # PID of the process running in the first pane
    remote: Optional[str]  # SSH target used at launch; None means local
    created_at: datetime   # session creation time (UTC)
    cmd: str               # normalised command string passed to tmux
    cwd: Optional[str]     # working directory used, or None


def _run(args: list[str], remote: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run *args* locally, or via ``ssh remote`` if *remote* is given.

    Remote commands are piped to ``$SHELL -l`` on the remote so the user's
    actual login shell (bash, zsh, fish …) sources its profile and the full
    PATH — including Homebrew, nix, conda, etc. — is available.  Using stdin
    also avoids the nested-quoting problem of ``ssh host 'cmd -l -c "…"'``.
    """
    if remote:
        return subprocess.run(
            ["ssh", remote, "$SHELL -l"],
            input=shlex.join(args) + "\n",
            capture_output=True, text=True, check=True,
        )
    return subprocess.run(args, capture_output=True, text=True, check=True)


def launch(
    cmd: str | list[str],
    name: Optional[str] = None,
    cwd: Optional[str] = None,
    remote: Optional[str] = None,
) -> SessionInfo:
    """Start *cmd* in a new detached tmux session.

    Args:
        cmd:    Command to run.  A list is joined into a shell command string.
        name:   tmux session name.  Auto-generated (``bsdb-<hex8>``) if None.
        cwd:    Working directory for *cmd* (remote path when *remote* is set).
        remote: SSH target such as ``"user@host"`` or ``"host"``.
                ``None`` runs tmux on the local machine.

    Returns:
        :class:`SessionInfo` carrying every identifier needed to reattach via
        :func:`attach` or to populate a tracking database row.

    Raises:
        subprocess.CalledProcessError: if tmux or ssh exits non-zero.
    """
    cmd_str = shlex.join(cmd) if isinstance(cmd, list) else cmd

    if name is None:
        name = f"bsdb-{uuid.uuid4().hex[:8]}"

    new_args: list[str] = ["tmux", "new-session", "-d", "-s", name]
    if cwd:
        new_args += ["-c", cwd]
    new_args.append(cmd_str)

    _run(new_args, remote)

    fmt = "#{session_id}|#{window_id}|#{pane_id}|#{pane_pid}|#{session_created}"
    result = _run(["tmux", "display-message", "-t", name, "-p", fmt], remote)

    # Take the last line in case login scripts wrote anything to stdout before
    # the tmux output.
    session_id, window_id, pane_id, pane_pid_str, created_ts = (
        result.stdout.strip().split("\n")[-1].split("|")
    )

    return SessionInfo(
        session_name=name,
        session_id=session_id,
        window_id=window_id,
        pane_id=pane_id,
        pane_pid=int(pane_pid_str),
        remote=remote,
        created_at=datetime.fromtimestamp(int(created_ts), tz=timezone.utc),
        cmd=cmd_str,
        cwd=cwd,
    )
